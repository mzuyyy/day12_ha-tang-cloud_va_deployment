"""Mock LLM used by the complete lab so Docker builds are self-contained."""
import random
import time


MOCK_RESPONSES = {
    "default": [
        "This is a mock AI response. In production this would come from a real LLM.",
        "The agent is running correctly and received your question.",
        "This cloud-ready agent can be protected, scaled, and monitored.",
    ],
    "docker": [
        "Docker packages the app and its dependencies into a portable container."
    ],
    "deploy": [
        "Deployment moves the application from a local machine to a server or cloud platform."
    ],
    "health": [
        "Health checks let the platform know whether the service should keep receiving traffic."
    ],
}


def ask(question: str, delay: float = 0.1) -> str:
    time.sleep(delay + random.uniform(0, 0.05))
    question_lower = question.lower()
    for keyword, responses in MOCK_RESPONSES.items():
        if keyword in question_lower:
            return random.choice(responses)
    return random.choice(MOCK_RESPONSES["default"])
