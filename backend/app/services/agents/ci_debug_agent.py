"""Role entry point for durable submission and one native investigation decision."""
from app.services.agents.base import BaseAgent
from app.services.agents.investigation import ROLE_PROMPTS, investigation_decision, investigation_tools


class CIDebugAgent(BaseAgent):
    name = 'ci_debug_agent'
    description = ROLE_PROMPTS['ci_debug']
    available_tools = list(investigation_tools('ci_debug')[1].values())

    async def decide(self, messages):
        return await investigation_decision('ci_debug', messages)

    async def run(self, input_data, context):
        from app.services.review_runs import submit_role
        return submit_role(context["db"], context["repo_id"], 'ci_debug', input_data, context.get("conversation"))
