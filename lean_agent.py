import asyncio
from dotenv import dotenv_values
from browser_use import Agent, Browser
from browser_use.llm import ChatAnthropic, ChatGoogle, ChatOpenAI


def load_config() -> dict[str, str]:
    """Load configuration strictly from .env file."""
    raw = dotenv_values(".env")
    return {k: v for k, v in raw.items() if isinstance(v, str) and v}


def get_llm(config: dict[str, str]):
    """Pick an LLM using only credentials present in .env."""
    anthropic_key = config.get("ANTHROPIC_API_KEY")
    google_key = config.get("GOOGLE_API_KEY")
    openai_key = config.get("OPENAI_API_KEY")

    if anthropic_key:
        model = config.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        print(f"Using Anthropic ({model}) from .env")
        return ChatAnthropic(model=model, api_key=anthropic_key)

    if google_key:
        model = config.get("GOOGLE_MODEL", "gemini-2.5-pro")
        print(f"Using Google ({model}) from .env")
        return ChatGoogle(model=model, api_key=google_key)

    if openai_key:
        model = config.get("OPENAI_MODEL", "gpt-5-mini")
        print(f"Using OpenAI ({model}) from .env")
        return ChatOpenAI(model=model, api_key=openai_key)

    raise ValueError(
        "No API key found in .env. Add one of ANTHROPIC_API_KEY, GOOGLE_API_KEY, or OPENAI_API_KEY."
    )


async def main():
    config = load_config()
    browser = Browser()
    llm = get_llm(config)
    task = config.get(
        "AGENT_TASK",
        "Go to all the market place and provide me the cheapest flight price from banglore to ranchi for next month",
    )

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
