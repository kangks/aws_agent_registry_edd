"""Deploy the Trajectory Evaluator Lambda and register as an AgentCore code-based evaluator.

Steps:
1. Package the Lambda code (boto3 is in the Lambda runtime, no third-party deps needed).
2. Create or update the Lambda function with the right IAM role.
3. Grant the AgentCore Evaluations service permission to invoke the Lambda.
4. Register a code-based evaluator pointing to the Lambda ARN.

All resources tagged: app=multiplier-hr-agent, project=eddpoc, env=dev.
"""
import json
import os
import time
import zipfile
from io import BytesIO
from pathlib import Path

import boto3

REGION = "us-east-1"
ACCOUNT_ID = "654654616949"
PROFILE = "ml-sandbox"

LAMBDA_NAME = "eddpoc-trajectory-evaluator"
LAMBDA_ROLE_NAME = "eddpoc-trajectory-evaluator-role"
EVALUATOR_NAME = "multiplier_trajectory_eval"
EVAL_SERVICE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"

TAGS = {
    "app": "multiplier-hr-agent",
    "project": "eddpoc",
    "env": "dev",
}

SCRIPT_DIR = Path(__file__).resolve().parent
LAMBDA_SOURCE = SCRIPT_DIR / "trajectory_evaluator_lambda.py"

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
iam = session.client("iam")
lam = session.client("lambda")
ac_control = session.client("bedrock-agentcore-control")


# ---------------------------------------------------------------------------
# 1. IAM Role
# ---------------------------------------------------------------------------

def ensure_iam_role() -> str:
    """Create or fetch the Lambda execution role with Bedrock invoke permissions."""
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    permissions_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": [
                    f"arn:aws:bedrock:{REGION}::foundation-model/*",
                    f"arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:inference-profile/*",
                    "arn:aws:bedrock:*:*:inference-profile/*",
                ],
            },
        ],
    }

    try:
        role = iam.get_role(RoleName=LAMBDA_ROLE_NAME)
        role_arn = role["Role"]["Arn"]
        print(f"✓ Role exists: {role_arn}")
        # Ensure policy is up to date
        iam.put_role_policy(
            RoleName=LAMBDA_ROLE_NAME,
            PolicyName="TrajectoryEvaluatorPolicy",
            PolicyDocument=json.dumps(permissions_policy),
        )
        return role_arn
    except iam.exceptions.NoSuchEntityException:
        print(f"Creating role: {LAMBDA_ROLE_NAME}")
        resp = iam.create_role(
            RoleName=LAMBDA_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Execution role for AgentCore trajectory evaluator Lambda",
            Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
        )
        iam.put_role_policy(
            RoleName=LAMBDA_ROLE_NAME,
            PolicyName="TrajectoryEvaluatorPolicy",
            PolicyDocument=json.dumps(permissions_policy),
        )
        print(f"✓ Role created: {resp['Role']['Arn']}")
        print("  Waiting 10s for role propagation...")
        time.sleep(10)
        return resp["Role"]["Arn"]


# ---------------------------------------------------------------------------
# 2. Lambda Package
# ---------------------------------------------------------------------------

def build_zip() -> bytes:
    """Build a Lambda deployment zip. Only pure-Python code — boto3 is in runtime."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(LAMBDA_SOURCE, arcname="lambda_function.py")
    buf.seek(0)
    return buf.read()


def ensure_lambda(role_arn: str) -> str:
    """Create or update the Lambda function."""
    code_zip = build_zip()
    print(f"✓ Built Lambda zip ({len(code_zip)} bytes)")

    try:
        current = lam.get_function(FunctionName=LAMBDA_NAME)
        # Update code
        lam.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=code_zip)
        # Wait for code update to finish
        lam.get_waiter("function_updated").wait(FunctionName=LAMBDA_NAME)
        # Update config (role, timeout, memory)
        lam.update_function_configuration(
            FunctionName=LAMBDA_NAME,
            Role=role_arn,
            Timeout=300,
            MemorySize=512,
            Environment={"Variables": {
                "BEDROCK_MODEL_ID": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "BEDROCK_REGION": REGION,
            }},
        )
        lam.get_waiter("function_updated").wait(FunctionName=LAMBDA_NAME)
        arn = current["Configuration"]["FunctionArn"]
        print(f"✓ Lambda updated: {arn}")
        return arn
    except lam.exceptions.ResourceNotFoundException:
        print(f"Creating Lambda: {LAMBDA_NAME}")
        resp = lam.create_function(
            FunctionName=LAMBDA_NAME,
            Runtime="python3.11",
            Role=role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": code_zip},
            Timeout=300,
            MemorySize=512,
            Environment={"Variables": {
                "BEDROCK_MODEL_ID": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "BEDROCK_REGION": REGION,
            }},
            Tags=TAGS,
        )
        lam.get_waiter("function_active").wait(FunctionName=LAMBDA_NAME)
        arn = resp["FunctionArn"]
        print(f"✓ Lambda created: {arn}")
        return arn


def grant_invoke_permission(lambda_arn: str) -> None:
    """Allow the AgentCore Evaluations service to invoke this Lambda."""
    statement_id = "AllowAgentCoreEvaluationsInvoke"
    try:
        lam.remove_permission(FunctionName=LAMBDA_NAME, StatementId=statement_id)
    except lam.exceptions.ResourceNotFoundException:
        pass
    lam.add_permission(
        FunctionName=LAMBDA_NAME,
        StatementId=statement_id,
        Action="lambda:InvokeFunction",
        Principal=EVAL_SERVICE_PRINCIPAL,
    )
    print(f"✓ Granted invoke permission to {EVAL_SERVICE_PRINCIPAL}")


# ---------------------------------------------------------------------------
# 3. AgentCore Evaluator
# ---------------------------------------------------------------------------

def ensure_evaluator(lambda_arn: str) -> dict:
    """Register the Lambda as an AgentCore code-based evaluator."""
    # Check for existing evaluator
    existing = None
    paginator = ac_control.get_paginator("list_evaluators")
    for page in paginator.paginate():
        for ev in page.get("evaluatorSummaries", page.get("evaluators", [])):
            if ev.get("evaluatorName") == EVALUATOR_NAME:
                existing = ev
                break
        if existing:
            break

    evaluator_config = {
        "codeBased": {
            "lambdaConfig": {
                "lambdaArn": lambda_arn,
                "lambdaTimeoutInSeconds": 300,
            }
        }
    }

    if existing:
        ev_id = existing.get("evaluatorId") or existing.get("id")
        print(f"✓ Evaluator exists: {ev_id}")
        # Optionally update (only if unlocked)
        try:
            ac_control.update_evaluator(
                evaluatorId=ev_id,
                evaluatorConfig=evaluator_config,
            )
            print(f"  ✓ Updated evaluator config")
        except Exception as e:
            print(f"  (update skipped: {type(e).__name__})")
        return {"evaluatorId": ev_id}

    print(f"Creating evaluator: {EVALUATOR_NAME}")
    resp = ac_control.create_evaluator(
        evaluatorName=EVALUATOR_NAME,
        level="TRACE",
        evaluatorConfig=evaluator_config,
        description="Trajectory-aware code-based evaluator for HR agent (Strands + tools)",
        tags=TAGS,
    )
    print(f"✓ Evaluator created: {resp.get('evaluatorId')}")
    print(f"  ARN: {resp.get('evaluatorArn')}")
    return resp


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("Deploying Trajectory Evaluator Lambda + AgentCore Evaluator")
    print("=" * 60)

    role_arn = ensure_iam_role()
    lambda_arn = ensure_lambda(role_arn)
    grant_invoke_permission(lambda_arn)
    result = ensure_evaluator(lambda_arn)

    print()
    print("=" * 60)
    print("DEPLOYMENT COMPLETE")
    print("=" * 60)
    print(f"Lambda ARN:     {lambda_arn}")
    print(f"Evaluator ID:   {result.get('evaluatorId')}")
    print(f"Evaluator ARN:  {result.get('evaluatorArn', '(run deploy again to fetch)')}")
