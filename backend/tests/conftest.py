import pytest

from friday.conversation.engine import ConversationEngine
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider("fake", reply="Sure. Here is a perfectly reasonable answer for you.")


@pytest.fixture
def router(fake: FakeProvider) -> ProviderRouter:
    return ProviderRouter({"fake": fake}, {"default": ["fake"]})


@pytest.fixture
def engine(router: ProviderRouter) -> ConversationEngine:
    return ConversationEngine(router, channel=Channel.TEXT)
