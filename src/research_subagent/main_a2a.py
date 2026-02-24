from .agent import agent

# Not compatible with Lambda deployment
app = agent.to_a2a(debug=True)
