# AWS Deployment & Development Guidelines

## AWS Deployment

- Always use **serverless** architecture for AWS deployments (Lambda, API Gateway, DynamoDB, S3, etc.)
- Do not use EC2, ECS, or EKS unless explicitly requested

## AWS Lambda Functions

- Do not include heavy ML Python libraries (e.g., PyTorch, TensorFlow, scikit-learn, pandas with large dependencies) in Lambda functions
- Use lightweight alternatives or offload heavy processing to SageMaker or Bedrock

## Python Environment

- Always use **Python 3.11** virtual environment for local development and Lambda runtime
- Set Lambda runtime to `python3.11`

## Git Workflow

- Perform a local `git commit` for major changes (e.g., new features, significant refactors, infrastructure updates)
- Use clear, descriptive commit messages summarizing what changed and why

## Terminal Safety

- **ALWAYS** create a temporary Python file (e.g., `tmp_script.py`) and run it with `python tmp_script.py` instead of using `python -c "..."` inline commands
- `python -c` can cause terminal buffer overflow and freeze the session
- Clean up temporary files after execution when appropriate
