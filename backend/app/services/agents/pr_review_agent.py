"""Role entry point for durable submission and one native investigation decision."""
from app.services.agents.base import BaseAgent
from app.services.agents.investigation import ROLE_PROMPTS, investigation_decision, investigation_tools


class PRReviewAgent(BaseAgent):
    name = 'pr_review_agent'
    description = ROLE_PROMPTS['pr_review']
    available_tools = list(investigation_tools('pr_review')[1].values())

    async def decide(self, messages):
        return await investigation_decision('pr_review', messages)

    async def run(self, input_data, context):
        from app.services.review_runs import submit_role
        return submit_role(context["db"], context["repo_id"], 'pr_review', input_data, context.get("conversation"))
