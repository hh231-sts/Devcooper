"""Role entry point for durable submission and one native investigation decision."""
from app.services.agents.base import BaseAgent
from app.services.agents.investigation import ROLE_PROMPTS, investigation_decision, investigation_tools


class IssueAgent(BaseAgent):
    name = 'issue_analyst_agent'
    description = ROLE_PROMPTS['issue_analysis']
    available_tools = list(investigation_tools('issue_analysis')[1].values())

    async def decide(self, messages):
        return await investigation_decision('issue_analysis', messages)

    async def run(self, input_data, context):
        from app.services.review_runs import submit_role
        return submit_role(context["db"], context["repo_id"], 'issue_analysis', input_data, context.get("conversation"))
