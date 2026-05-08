"""
Setup AWS Agent Registry for EDD POC.

Creates 6 registry records (CUSTOM type) in registry Rqbs73eeqpMEEwf9,
submits each for approval, then approves them.
Saves registry info to registry/registry_info.json.

Usage:
    AWS_PROFILE=ml-sandbox AWS_REGION=us-east-1 \
        .venv/bin/python scripts/setup_registry.py
"""

import json
import time
import boto3
from pathlib import Path

REGION = "us-east-1"
REGISTRY_ID = "Rqbs73eeqpMEEwf9"

# All 6 agents to register
AGENTS = [
    {
        "name": "multiplier-hr-sonnet-managed",
        "description": "HR/Compliance agent using Claude Sonnet 4, deployed on AgentCore Runtime",
        "model_id": "us.anthropic.claude-sonnet-4-6",
        "model_key": "sonnet",
        "deployment_path": "managed",
        "service_name": "eddpoc_multiplier_hr_sonnet.DEFAULT",
        "tools": ["employee_lookup", "compliance_checker", "payroll_calculator", "leave_manager"],
    },
    {
        "name": "multiplier-hr-haiku-managed",
        "description": "HR/Compliance agent using Claude Haiku 4.5, deployed on AgentCore Runtime",
        "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "model_key": "haiku",
        "deployment_path": "managed",
        "service_name": "eddpoc_multiplier_hr_haiku.DEFAULT",
        "tools": ["employee_lookup", "compliance_checker", "payroll_calculator", "leave_manager"],
    },
    {
        "name": "multiplier-hr-nova-pro-managed",
        "description": "HR/Compliance agent using Amazon Nova Pro, deployed on AgentCore Runtime",
        "model_id": "us.amazon.nova-pro-v1:0",
        "model_key": "nova_pro",
        "deployment_path": "managed",
        "service_name": "eddpoc_multiplier_hr_nova_pro.DEFAULT",
        "tools": ["employee_lookup", "compliance_checker", "payroll_calculator", "leave_manager"],
    },
    {
        "name": "multiplier-hr-sonnet-byo",
        "description": "HR/Compliance agent using Claude Sonnet 4, BYO with ADOT -> CloudWatch",
        "model_id": "us.anthropic.claude-sonnet-4-6",
        "model_key": "sonnet",
        "deployment_path": "byo",
        "service_name": "multiplier-byo-sonnet",
        "tools": ["employee_lookup", "compliance_checker", "payroll_calculator", "leave_manager"],
    },
    {
        "name": "multiplier-hr-haiku-byo",
        "description": "HR/Compliance agent using Claude Haiku 4.5, BYO with ADOT -> CloudWatch",
        "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "model_key": "haiku",
        "deployment_path": "byo",
        "service_name": "multiplier-byo-haiku",
        "tools": ["employee_lookup", "compliance_checker", "payroll_calculator", "leave_manager"],
    },
    {
        "name": "multiplier-hr-nova-pro-byo",
        "description": "HR/Compliance agent using Amazon Nova Pro, BYO with ADOT -> CloudWatch",
        "model_id": "us.amazon.nova-pro-v1:0",
        "model_key": "nova_pro",
        "deployment_path": "byo",
        "service_name": "multiplier-byo-nova-pro",
        "tools": ["employee_lookup", "compliance_checker", "payroll_calculator", "leave_manager"],
    },
]


def main():
    session = boto3.Session(profile_name="ml-sandbox", region_name=REGION)
    client = session.client("bedrock-agentcore-control")

    print("=" * 60)
    print(f"Registering 6 agents in AWS Agent Registry: {REGISTRY_ID}")
    print("=" * 60)

    record_ids = []
    record_arns = []

    # Step 1: Create registry records
    print("\n[Step 1] Creating registry records...")
    for agent in AGENTS:
        print(f"\n  Creating: {agent['name']}")

        custom_metadata = json.dumps({
            "agent_name": agent["name"],
            "model_id": agent["model_id"],
            "model_key": agent["model_key"],
            "deployment_path": agent["deployment_path"],
            "service_name": agent["service_name"],
            "tools": agent["tools"],
            "last_eval_score": None,
            "last_eval_date": None,
        })

        try:
            response = client.create_registry_record(
                registryId=REGISTRY_ID,
                name=agent["name"],
                description=agent["description"],
                descriptorType="CUSTOM",
                descriptors={
                    "custom": {
                        "inlineContent": custom_metadata,
                    }
                },
                recordVersion="1.0.0",
            )
            record_arn = response.get("recordArn", "")
            # Extract record ID from ARN: .../record/<recordId>
            record_id = record_arn.split("/record/")[-1] if "/record/" in record_arn else response.get("recordId", "")
            record_ids.append(record_id)
            record_arns.append(record_arn)
            print(f"    ✅ Record created: {record_id}")
            print(f"    ARN: {record_arn}")
            print(f"    Status: {response.get('status', 'unknown')}")
        except Exception as e:
            err_str = str(e).lower()
            if "already exists" in err_str or "conflict" in err_str:
                print(f"    ⚠️  Already exists, skipping: {e}")
                record_ids.append(None)
                record_arns.append(None)
            else:
                print(f"    ❌ Failed: {e}")
                record_ids.append(None)
                record_arns.append(None)

    # Step 2: Wait for records to transition to DRAFT
    print("\n[Step 2] Waiting for records to reach DRAFT status...")
    time.sleep(5)

    for agent, record_id in zip(AGENTS, record_ids):
        if not record_id:
            continue
        for attempt in range(15):
            try:
                rec = client.get_registry_record(registryId=REGISTRY_ID, recordId=record_id)
                status = rec.get("status", "")
                if status == "DRAFT":
                    print(f"    {agent['name']}: DRAFT ✅")
                    break
                print(f"    {agent['name']}: {status} (waiting...)")
                time.sleep(2)
            except Exception as e:
                print(f"    {agent['name']}: error checking status: {e}")
                time.sleep(2)
        else:
            print(f"    {agent['name']}: ⚠️  Did not reach DRAFT after 30s")

    # Step 3: Submit for approval
    print("\n[Step 3] Submitting records for approval...")
    for agent, record_id in zip(AGENTS, record_ids):
        if not record_id:
            continue
        print(f"  Submitting: {agent['name']}...", end=" ", flush=True)
        try:
            response = client.submit_registry_record_for_approval(
                registryId=REGISTRY_ID,
                recordId=record_id,
            )
            print(f"✅ Status: {response.get('status', 'unknown')}")
        except Exception as e:
            print(f"❌ {e}")

    # Step 4: Approve all records
    print("\n[Step 4] Approving records...")
    time.sleep(3)

    for agent, record_id in zip(AGENTS, record_ids):
        if not record_id:
            continue
        print(f"  Approving: {agent['name']}...", end=" ", flush=True)
        try:
            response = client.update_registry_record_status(
                registryId=REGISTRY_ID,
                recordId=record_id,
                status="APPROVED",
                statusReason="Initial registration for EDD POC evaluation",
            )
            print(f"✅ Status: {response.get('status', 'unknown')}")
        except Exception as e:
            print(f"❌ {e}")

    # Save registry info
    registry_info = {
        "registry_id": REGISTRY_ID,
        "registry_arn": f"arn:aws:bedrock-agentcore:{REGION}:654654616949:registry/{REGISTRY_ID}",
        "records": [],
    }

    for agent, record_id, record_arn in zip(AGENTS, record_ids, record_arns):
        registry_info["records"].append({
            "name": agent["name"],
            "record_id": record_id,
            "record_arn": record_arn,
            "model_key": agent["model_key"],
            "deployment_path": agent["deployment_path"],
            "service_name": agent["service_name"],
        })

    out_path = Path(__file__).resolve().parent.parent / "registry" / "registry_info.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(registry_info, f, indent=2)
        f.write("\n")

    print(f"\n{'=' * 60}")
    print("COMPLETE")
    print(f"{'=' * 60}")
    registered = sum(1 for r in record_ids if r)
    print(f"  Registered: {registered}/6 agents")
    print(f"  Registry ID: {REGISTRY_ID}")
    print(f"  Info saved: {out_path}")


if __name__ == "__main__":
    main()
