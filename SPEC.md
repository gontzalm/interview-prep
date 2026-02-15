# Interview Prep Agent

## Executive Summary

Development of a distributed **Agentic System** for job interview preparation.

**Architecture Pattern:** Actor Model with Orchestrator (Backend) and
Specialists (MCP/A2A).

- **Deployment Strategy:** Hybrid (for local development) and Cloud (for
  production environment).
- **Compute:** Local Docker Compose (for local development) and AWS Lambda (for
  production environment)
- **Persistence:** Real AWS Resources (S3, DynamoDB, Cognito) deployed via CDK.

---

## Directory Structure

- `infra/`: AWS CDK code (Stacks & Constructs).
- `ui/`: **Chainlit** Frontend (Auth + Chat + Persistence).
- `src/agent/`: **Main Backend API** (FastAPI + Pydantic AI).
- `src/tools/`: **MCP Server** (FastMCP).
- `src/research_subagent/`: **A2A Specialist** (Pydantic AI).

---

## Infrastructure & Persistence (CDK)

### Custom Construct: `LwaLambdaFunction`

Wraps AWS Lambda Web Adapter to deploy web frameworks as Lambda functions using Docker.

- **Parameters:** `use_apigw` (bool), `streaming_response` (bool), `uv_group` (str), `src_dirs` (list), `cmd_parts` (list).
- **Architecture:** ARM_64.
- **Logic:**
  - If `streaming_response=True`, sets env `AWS_LWA_INVOKE_MODE=RESPONSE_STREAM`.
  - If `use_apigw=True`, fronts the Lambda with API Gateway and a Cognito User Pools Authorizer.
  - If `use_apigw=False`, uses Function URL (`add_function_url`) with `InvokeMode.RESPONSE_STREAM` (if streaming) or `BUFFERED` and `AWS_IAM` authentication.

### Stacks

#### `InterviewPrepStack(app, "interview-prep-stack-local", local_dev=True, env=cdk.Environment)`

Shared resources for local development.

- **Removal Policy:** `DESTROY` (including `auto_delete_objects` for S3).
- **Cognito User Pool:** Standard sign-in with email. Creates a test user `test@example.com` (verified) for development.
- **S3 Buckets:**
  - `chainlit-bucket`: For Chainlit state persistence.
  - `storage-bucket`: For resume and prep document storage.
- **DynamoDB Table:** `chainlit-table` with strict schema for `DynamoDBDataLayer`.
  - **Partition Key:** `PK` (String)
  - **Sort Key:** `SK` (String)
  - **GSI:** `UserThread` (PK: `UserThreadPK`, SK: `UserThreadSK`).
- **Output:** Generates a `local-dotenv-output` containing resource IDs and secrets.

#### `InterviewPrepStack(app, "interview-prep-stack", env=cdk.Environment)`

Production deployment.

- **Removal Policy:** `RETAIN`.
- **Lambdas:**
  - `agent-lambda`: Main backend (FastAPI), streaming enabled, fronted by API Gateway.
  - `mcp-lambda`: MCP server (FastMCP), fronted by Function URL (IAM auth).
  - `subagent-lambda`: Research specialist (Pydantic AI A2A), fronted by Function URL (IAM auth).
- **ECS Fargate:** Deploys the Chainlit UI as a load-balanced service.
- **CloudFront:** Fronts the ECS service for HTTPS and global distribution.

---

## Component Specifications

### Frontend (Chainlit)

**Reference:** `ui/app.py`

- **Data Layer:** `DynamoDBDataLayer` + `S3StorageClient`.
- **Auth:** Cognito OAuth2. Sets up session with user email and token.
- **Communication:**
  - Sends `POST /chat` to Backend.
  - Payload: `user_email`, `message`, `chat_history_json`, `resume_bytes_b64`.
  - Consumes SSE (Server-Sent Events) via `httpx-sse`.
- **Persistence:** Handles `on_chat_resume` to reconstruct `chat_history` from thread steps.

**Environment Variables:**

| Name | Description |
| --- | --- |
| `CHAINLIT_URL` | Application URL (CloudFront domain) |
| `CHAINLIT_AUTH_SECRET` | Secret for Chainlit authentication |
| `OAUTH_COGNITO_CLIENT_ID` | Cognito App Client ID |
| `OAUTH_COGNITO_CLIENT_SECRET`| Cognito App Client Secret |
| `OAUTH_COGNITO_DOMAIN` | Cognito Domain (without https://) |
| `OAUTH_COGNITO_SCOPE` | OAuth scopes (openid profile email mcp/mcp) |
| `CHAINLIT_BUCKET` | S3 bucket for data persistence |
| `CHAINLIT_TABLE` | DynamoDB table for data persistence |
| `BACKEND_URL` | URL of the Agent API Gateway |

### Main Backend (FastAPI + Pydantic AI)

**Reference:** `src/agent/agent.py`

- **Model:** `anthropic.claude-haiku-4-5-20251001-v1:0` (via Bedrock).
- **Context Management:** `process_history` keeps last 20 messages, ensuring tool call/result parity and starting with a human request.
- **Resume Handling:** If resume bytes are present in request, prepends instruction to LLM and includes `BinaryContent` (PDF) in the first message.
- **Streaming:** Uses `agent.run_stream_events` to yield SSE events.

**Environment Variables:**

| Name | Description |
| --- | --- |
| `AGENT_MODEL` | Bedrock model ID |
| `MCP_URL` | URL of the MCP server |
| `LOGFIRE_SECRET` | Secrets Manager ID for Logfire token |
| `CORS_ALLOW_ORIGINS` | Allowed CORS origins (comma-separated) |

### MCP Server (Tools Layer)

**Reference:** `src/tools/main.py`

- **Framework:** FastMCP (HTTP Transport).
- **Tools:**
  - `get_resume()`: Fetches plain text resume from `s3://{bucket}/{email}/resume.txt`.
  - `upload_resume(content)`: Saves provided text `content` to `s3://{bucket}/{email}/resume.txt`.
  - `list_preps()`: Returns `list[InterviewPrepMetadata]` with S3 presigned URLs.
  - `generate_prep(job_description)`: Calls Research Subagent via `A2AClient`, polls for completion, converts Markdown to PDF using `md2pdf` and `pdf-styles.css`, and uploads to S3.

**Environment Variables:**

| Name | Description |
| --- | --- |
| `STORAGE_BUCKET` | S3 bucket for resumes and preps |
| `RESEARCH_SUBAGENT_URL` | URL of the Research Subagent |

### A2A Specialist (Research Layer)

**Reference:** `src/research_subagent/main.py`

- **Framework:** Pydantic AI (A2A mode).
- **Model:** `anthropic.claude-opus-4-6-v1` (via Bedrock).
- **Tools:** `research_company(query)` uses `TavilyClient` for real-time web search.
- **Output:** Structured Markdown report following a strict template in instructions.

**Environment Variables:**

| Name | Description |
| --- | --- |
| `RESEARCH_SUBAGENT_MODEL` | Bedrock model ID |
| `TAVILY_SECRET` | Secrets Manager ID for Tavily API key |
| `LOGFIRE_SECRET` | Secrets Manager ID for Logfire token |

---

## SSE Protocol Definition

| Event Type | Data Payload | UI Action |
| --- | --- | --- |
| `token` | `{"text": "..."}` | Streams text to chat. |
| `tool_call` | `{"name": "...", "args": "..."}` | Renders a `cl.Step`. |
| `pdf_generated` | `{"url": "..."}` | Renders `cl.Pdf` (inline). |
| `prep_list` | `{"preps": [...]}` | Renders a Markdown table with download links. |
| `error` | `{"message": "..."}` | Displays error message. |

---

## Auth & Security

- **End-to-End Identity:** User email is extracted from Cognito JWT in the UI, passed to the Backend, and then propagated to MCP via `X-User-Email` header.
- **Authorization:**
  - Backend API Gateway: Cognito User Pool Authorizer.
  - MCP & Subagent: AWS IAM (granting `lambda:InvokeFunctionUrl` to the caller).
  - S3/DynamoDB: IAM Roles (Task Role for ECS, Execution Role for Lambda).
- **Logfire:** Centralized observability for Agent and Subagent.

---

## Development Workflow

- **Local:**
  - `infra/stack.py` (local_dev=True) -> `local.env`.
  - `compose.yaml` links all services.
  - AWS credentials mounted via `~/.aws`.
- **Production:**
  - `infra/stack.py` (local_dev=False) -> AWS deployment.
  - Dockerfiles dynamically generated in `infra/dockerfiles/` for Lambda functions.
  - UI deployed via ECS Fargate and CloudFront.
