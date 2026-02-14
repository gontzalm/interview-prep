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
- `src/research-subagent/`: **A2A Specialist** (Pydantic AI).

---

## Infrastructure & Persistence (CDK)

### Custom Construct: `LwaLambdaFunction`

Wrap AWS Lambda Web Adapter. Adapt existing construct to requirements.

- **Parameters:** `use_apigw` (bool), `streaming_response` (bool).
- **Logic:**
  - If `streaming_response=True`, set env `AWS_LWA_INVOKE_MODE=RESPONSE_STREAM`.
  - If `use_apigw=False`, use Function URL (`add_function_url`) with
    `InvokeMode.RESPONSE_STREAM` (if streaming) or `BUFFERED`.

### Stacks

#### `InterviewPrepStack(app, "interview-prep-stack-local", local_dev=True, env=cdk.Environment)`

All the constructs should have a CDK destroy policy.

- **Cognito User Pool:** Standard OAuth2 configuration. For local development
  only, create a pre-approved user `test@example.com` with the password "test".
- **S3 Buckets:** Versioned, private. One for Chainlit persistence and another
  for the Backend agent storage.
- **DynamoDB Table:** **STRICT Schema** for Chainlit Data Layer.
  - **Partition Key:** `PK` (String)
  - **Sort Key:** `SK` (String)
  - **GSI:** Name `UserThread`, PK `UserThreadPK`, SK `UserThreadSK`.
  - _Note:_ This schema is mandatory for `DynamoDBDataLayer` to work.
- **Secrets Manager Secrets:** Import the `subagent/tavily/api-key` secret, it
  is already created.
- **Output:** Auto-generate `local.env` file with resource IDs.

#### `InterviewPrepStack(app, "interview-prep-stack", env=cdk.Environment)`

All the constructs should have a CDK retain policy.

Deploy all the constructs defined in the local stack, and `if local_dev: return`
else:

- Create the 3 Lambdas using `LwaLambdaFunction`
- Deploy the Chainlit app to AWS ECS Fargate + Cloudfront using the same
  strategy already implemented (adapt it).

---

## Component Specifications

### Frontend (Chainlit)

**Reference:** `https://docs.chainlit.io/llms.txt`

- **Data Layer:** MUST use `DynamoDBDataLayer` + `S3StorageClient`.
  - _Constraint:_ Do NOT implement custom DB logic in the UI. Use the native
    layer to persist chat history automatically. There is already some
    scaffolding in the existing chainlint app you can adapt/reuse. The idea is
    to use the Chainlit conversation (Thread) to pass the conversation history
    to the `/chat` endpoint.
- **Auth:** Chainlit Native Authentication (Cognito OAuth). Already implemented.
- **Communication:**
  - Sends user message to Backend `POST /chat`.
  - Sends user uploaded PDF resume bytes to `POST /chat` if the user attaches a
    PDF.
  - Headers: `Authorization: Bearer <token>`.
  - Consumes **SSE (Server-Sent Events)** for real-time text & artifacts. Use
    existing pattern.

**Environment Variables:**

| Name                          | Description                                  | Local Dev   | Production                                                              |
| ----------------------------- | -------------------------------------------- | ----------- | ----------------------------------------------------------------------- |
| `CHAINLIT_URL`                | Cloudfront distribution domain URL           | Not set     | Injected via construct env vars (`default_container.add_environment()`) |
| `CHAINLIT_AUTH_SECRET`        | `secrets.token_hex(64)`                      | `local.env` | Injected via construct env vars (`default_container.add_environment()`) |
| `OAUTH_COGNITO_CLIENT_ID`     | User pool client ID                          | `local.env` | Injected via construct env vars (`default_container.add_environment()`) |
| `OAUTH_COGNITO_CLIENT_SECRET` | User pool client secret                      | `local.env` | Injected via construct env vars (`default_container.add_environment()`) |
| `OAUTH_COGNITO_DOMAIN`        | User pool domain                             | `local.env` | Injected via construct env vars (`default_container.add_environment()`) |
| `OAUTH_COGNITO_SCOPE`         | OAuth scope (`openid profile email`)         | `local.env` | Injected via construct env vars (`default_container.add_environment()`) |
| `CHAINLIT_BUCKET`             | S3 bucket for Chainlit data persistence      | `local.env` | Injected via construct env vars (`default_container.add_environment()`) |
| `CHAINLIT_TABLE`              | DynamoDB table for Chainlit data persistence | `local.env` | Injected via construct env vars (`default_container.add_environment()`) |
| `BACKEND_URL`                 | URL of the main backend (API Gateway)        | `local.env` | Injected via construct env vars (`default_container.add_environment()`) |

### Main Backend (FastAPI + Pydantic AI)

**Reference:** `https://ai.pydantic.dev/llms.txt`

- **Framework:**: FastAPI + Pydantic AI
- **Role:** Stateless orchestrator agent. Write the instructions for the agent,
  enforcing a strict behaviour for the given task.
- **Model:**: `bedrock:anthropic.claude-sonnet-4-5-20250929-v1:0`
- **Context Management (The Adapter Pattern):** It receives the user session
  chat history in plain json and converts it to a list of `ModelMessage` via an
  adapter default factory.
- **Token Management:** Implement a `history_processor` to keep as maximum the
  last N (20) messages. Make sure the first message in the history is always a
  `ModelRequest` (human message). When slicing the message history, you need to
  make sure that tool calls and returns are paired, otherwise the LLM may return
  an error.

- **Tools:**
  - Configures `MCPServerStreamableHTTP` pointing to the MCP Service URL (Docker
    service name `http://mcp:8000/mcp` in local).
  - **Identity Propagation:** Injects `X-User-Email` header into the MCP
    transport for context propagation using the email extracted from the Cognito
    JWT.

**Environment Variables:**

| Name          | Description                                                            | Local Dev   | Production                   |
| ------------- | ---------------------------------------------------------------------- | ----------- | ---------------------------- |
| `AGENT_MODEL` | Bedrock agent model to use (`agent_model.model_arn.partition("/")[2]`) | `local.env` | Injected via Lambda env vars |
| `MCP_URL`     | URL of the MCP server (Lambda Function URL)                            | `local.env` | Injected via Lambda env vars |

### MCP Server (Tools Layer)

**Reference:**: `https://gofastmcp.com/llms.txt`

- **Framework:** FastMCP.
- **Role**: Toolbox for the main agent.
- **Security:** Trusts `X-User-Email` header (Internal Network/IAM).
- **Resources**: A single resource `resource://resume` with the current user
  resume. If it does not exist in S3, return a message indicating the agent that
  it shuld ask for the PDF resume to the user.
- **Tools:**
  - `upload_resume(bytes)`: Save to `s3://{bucket}/{email}/resume.pdf`. Uses
    PyPDF to convert the PDF bytes to plain text and save to
    `s3://{bucket}/{email}/resume.txt`.

  - `list_preps()`: List `s3://{bucket}/{email}/preps/` and return a list of
    `InterviewPrepMetadata` with doc title, datetime, and Presigned URLs.

  - `generate_prep(job_desc)`:
    - Fetch resume text from resource. If it does not exist, return right away.
    - Call **A2A Agent**. Use the `fasta2a.A2AClient` to communicate. Use
      `https://ai.pydantic.dev/api/fasta2a/#fasta2a.client.A2AClient` as
      reference.
    - Convert Markdown -> PDF (`md2pdf`).
    - Upload to `s3://{bucket}/{email}/preps/{company-position}.pdf`.
    - Return Presigned URL.

**Environment Variables:**

| Name                      | Description                                                                                         | Local Dev   | Production                   |
| ------------------------- | --------------------------------------------------------------------------------------------------- | ----------- | ---------------------------- |
| `STORAGE_BUCKET`          | S3 bucket to store preps and resumes                                                                | `local.env` | Injected via Lambda env vars |
| `RESEARCH_SUBAGENT_MODEL` | Bedrock agent model to use for the subagent (`research_subagent_model.model_arn.partition("/")[2]`) | `local.env` | Injected via Lambda env vars |

### A2A Specialist (Research Layer)

- **Role:** Pure logic (Deep Research).
- **Model:**: `bedrock:anthropic.claude-opus-4-6-v1`
- **Tools:** A `research_company` tool that uses `TavilyClient`.
- **Input:** Resume text + Job Description.
- **Output:** Structured markdown strategy, following the provided template in
  the instructions.

**Environment Variables:**

| Name            | Description                                          | Local Dev   | Production                   |
| --------------- | ---------------------------------------------------- | ----------- | ---------------------------- |
| `TAVILY_SECRET` | Name of the Tavily API Key secret in secrets manager | `local.env` | Injected via Lambda env vars |

---

## SSE Protocol Definition

The Backend must stream events to Chainlit using `text/event-stream`.

| Event Type      | Data Payload                      | UI Action                                                                            |
| --------------- | --------------------------------- | ------------------------------------------------------------------------------------ |
| `token`         | `{"text": "..."}`                 | Stream text to chat bubble.                                                          |
| `tool_call`     | `{"name": "...", "args": "..."}`  | Render a `cl.Step(name=data["name"], type="tool")` with `step.input = data["args"]`. |
| `pdf_generated` | `{"url": "..."}`                  | Render `cl.Pdf` inline.                                                              |
| `prep_list`     | `[{"name": "...", "url": "..."}]` | Render `cl.DataFrame` inline.                                                        |
| `error`         | `{"message": "..."}`              | Inform the user about the error.                                                     |

---

## Auth Flow

- The user logs in via Cognito OAuth integration with Chainlit.
- The Backend is protected with a Cognito Pool Authorizer that points to the
  created user pool.
- Configure CORS using the existing logic in `constructs.py` (i.e. the CORS are
  configured via `CORSMiddleware` and via env variable).
- Chainlit passes the Authorization header to the Backend via the `httpx`
  client.
- The backend decodes the user email from the access token, and passes it via
  header to the MCP server.
- The MCP server is protected by IAM (`mcp_server.grant_invoke_url(agent)`) in
  production.
- The subagent is protected by IAM (`subagent.grant_invoke_url(mcp_server)`) in
  production.

---

## Implementation Plan (Step-by-Step)

### Phase 1: Infrastructure & Environment

#### Production

- Implement/adapt CDK `app.py`, `stack.py` (DynamoDB/S3/Cognito), and
  `constructs.py`. Output dockerfiles for CDK `DockerImageFunction`s should be
  saved in `infra/dockerfiles/{component}.Dockerfile`, which is gitignored as
  this is dynamically generated for the production deployment. The dependencies
  are installed specifying uv groups (i.e. `uv install --group ui`). Use the
  following base image for the Dockerfiles:

  ```docker
  FROM python:3.13
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
  ```

- In the production deployment, just pass dynamic env variables (i.e.
  `{"BACKEND_URL": agent.function.function_url}`) to connect components.

- For now, do not try to deploy the production stack, just define it.

#### Local

- Deploy local stack -> Generate `local.env`.

- Create `Dockerfile`s for each component inside the `src/{component}`
  directory. This are "static" and version controlled. The dependencies are
  installed specifying uv groups (i.e. `uv install --group ui`). Use the
  following base image for the Dockerfiles:

  ```docker
  FROM python:3.13
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
  ```

- Create `compose.yaml` linking `ui`, `backend`, `mcp`, `research-subagent`,
  passing the necessary environment variables along (some will be in `local.env`
  and others are passed like `BACKEND_URL: http://agent:8000`).

- For AWS auth, bind mount the `~/.aws` directory in all the services (use a
  named mount). This directory contains admin credentials suitable for
  development.

### Phase 2: Core Backend & Logic

- Implement `src/tools` (MCP).
- Implement `src/research-subagent` (Tavily).
- Implement `src/agent` (FastAPI). This is almost already done.

### Phase 3: Frontend Integration

- Setup Chainlit with data persistence and auth.
- Implement `on_message` loop (Stream consumer).
- The chat history management is almost done, just adapt it.

---

## Development Rules

- **Strict Separation:** The UI never talks to S3 directly (except via Presigned
  URLs generated by MCP).

- **Reasonable Assumptions:** Make reasonable assumptions for missing or
  unspecified specifications.

- **No React:** Use Python-only Chainlit.

- **Secrets:** The Tavily API Key is read from AWS Secrets Manager.

- **Style**: Use modern Python features (typing). Use Python 3.13. Use google
  docstrings for public interfaces. Adhere to preexisting code style.
