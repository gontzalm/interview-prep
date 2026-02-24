from fastapi import FastAPI
from pydantic import BaseModel

from .agent import agent

app = FastAPI()


class ResearchRequest(BaseModel):
    query: str


@app.post("/")
async def research(request: ResearchRequest):
    result = await agent.run(request.query)
    preamble, heading_symbol, result = result.output.partition("#")
    return {"result": heading_symbol + result}
