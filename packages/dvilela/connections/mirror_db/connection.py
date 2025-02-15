#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2021-2024 David Vilela Freire
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""MirrorDB connection."""

import asyncio
import json
from typing import Any, Dict, Optional, Union, cast

import aiohttp
from aea.configurations.base import PublicId
from aea.connections.base import Connection, ConnectionStates
from aea.mail.base import Envelope
from aea.protocols.base import Address, Message
from aea.protocols.dialogue.base import Dialogue
from aea.protocols.dialogue.base import Dialogue as BaseDialogue

from packages.valory.protocols.srr.dialogues import SrrDialogue
from packages.valory.protocols.srr.dialogues import SrrDialogues as BaseSrrDialogues
from packages.valory.protocols.srr.message import SrrMessage


PUBLIC_ID = PublicId.from_str("dvilela/mirror_db:0.1.0")


class SrrDialogues(BaseSrrDialogues):
    """A class to keep track of SRR dialogues."""

    def __init__(self, **kwargs: Any) -> None:
        """
        Initialize dialogues.

        :param kwargs: keyword arguments
        """

        def role_from_first_message(  # pylint: disable=unused-argument
            message: Message, receiver_address: Address
        ) -> Dialogue.Role:
            """Infer the role of the agent from an incoming/outgoing first message

            :param message: an incoming/outgoing first message
            :param receiver_address: the address of the receiving agent
            :return: The role of the agent
            """
            return SrrDialogue.Role.CONNECTION

        BaseSrrDialogues.__init__(
            self,
            self_address=str(kwargs.pop("connection_id")),
            role_from_first_message=role_from_first_message,
            **kwargs,
        )


class MirrorDBConnection(Connection):
    """Proxy to the functionality of the mirror DB backend service."""

    connection_id = PUBLIC_ID

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the connection."""
        super().__init__(*args, **kwargs)
        self.base_url = self.configuration.config.get("mirror_db_base_url")
        self.api_key: Optional[str] = None
        self.agent_id: Optional[str] = None
        self.twitter_user_id: Optional[str] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.dialogues = SrrDialogues(connection_id=PUBLIC_ID)
        self._response_envelopes: Optional[asyncio.Queue] = None
        self.task_to_request: Dict[asyncio.Future, Envelope] = {}

    async def update_api_key(self, api_key: str) -> None:
        """Update the API key."""
        self.api_key = api_key

    async def update_agent_id(self, agent_id: str) -> None:
        """Update the agent ID."""
        self.agent_id = agent_id

    async def update_twitter_user_id(self, twitter_user_id: str) -> None:
        """Update the Twitter user ID."""
        self.twitter_user_id = twitter_user_id

    @property
    def response_envelopes(self) -> asyncio.Queue:
        """Returns the response envelopes queue."""
        if self._response_envelopes is None:
            raise ValueError(
                "`MirrorDBConnection.response_envelopes` is not yet initialized. Is the connection setup?"
            )
        return self._response_envelopes

    async def connect(self) -> None:
        """Connect to the backend service."""
        self._response_envelopes = asyncio.Queue()
        self.session = aiohttp.ClientSession()
        self.state = ConnectionStates.connected

    async def disconnect(self) -> None:
        """Disconnect from the backend service."""
        if self.is_disconnected:
            return

        self.state = ConnectionStates.disconnecting

        for task in self.task_to_request.keys():
            if not task.cancelled():
                task.cancel()
        self._response_envelopes = None

        if self.session is not None:
            await self.session.close()
            self.session = None

        self.state = ConnectionStates.disconnected

    async def receive(
        self, *args: Any, **kwargs: Any
    ) -> Optional[Union["Envelope", None]]:
        """Receive an envelope."""
        return await self.response_envelopes.get()

    async def send(self, envelope: Envelope) -> None:
        """Send an envelope."""
        task = self._handle_envelope(envelope)
        task.add_done_callback(self._handle_done_task)
        self.task_to_request[task] = envelope

    def _handle_envelope(self, envelope: Envelope) -> asyncio.Task:
        """Handle incoming envelopes by dispatching background tasks."""
        message = cast(SrrMessage, envelope.message)
        dialogue = self.dialogues.update(message)
        task = self.loop.create_task(self._get_response(message, dialogue))
        return task

    def prepare_error_message(
        self, srr_message: SrrMessage, dialogue: Optional[BaseDialogue], error: str
    ) -> SrrMessage:
        """Prepare error message"""
        response_message = cast(
            SrrMessage,
            dialogue.reply(  # type: ignore
                performative=SrrMessage.Performative.RESPONSE,
                target_message=srr_message,
                payload=json.dumps({"error": error}),
                error=True,
            ),
        )
        return response_message

    def _handle_done_task(self, task: asyncio.Future) -> None:
        """Process a done receiving task."""
        request = self.task_to_request.pop(task)
        response_message: Optional[Message] = task.result()

        response_envelope = None
        if response_message is not None:
            response_envelope = Envelope(
                to=request.sender,
                sender=request.to,
                message=response_message,
                context=request.context,
            )

        self.response_envelopes.put_nowait(response_envelope)

    async def _get_response(
        self, srr_message: SrrMessage, dialogue: Optional[BaseDialogue]
    ) -> SrrMessage:
        """Get response from the backend service."""
        if srr_message.performative != SrrMessage.Performative.REQUEST:
            return self.prepare_error_message(
                srr_message,
                dialogue,
                f"Performative `{srr_message.performative.value}` is not supported.",
            )

        payload = json.loads(srr_message.payload)
        method_name = payload.get("method")
        method = getattr(self, method_name, None)

        if method is None:
            return self.prepare_error_message(
                srr_message,
                dialogue,
                f"Method {method_name} is not available.",
            )

        try:
            response = await method(**payload.get("kwargs", {}))
            response_message = cast(
                SrrMessage,
                dialogue.reply(  # type: ignore
                    performative=SrrMessage.Performative.RESPONSE,
                    target_message=srr_message,
                    payload=json.dumps({"response": response}),
                    error=False,
                ),
            )
            return response_message

        except Exception as e:
            return self.prepare_error_message(
                srr_message, dialogue, f"Exception while calling backend service:\n{e}"
            )

    async def create_agent(self, agent_data: Dict) -> Dict:
        """Create an agent and a Twitter account."""
        async with self.session.post(  # type: ignore
            f"{self.base_url}/api/agents/",
            json=agent_data,
            headers={"access-token": f"{self.api_key}"},
        ) as response:
            agent_response = await response.json()

        agent_id = agent_response.get("agent_id")
        if agent_id is None:
            raise ValueError("Failed to create agent, no agent_id returned.")

        return agent_response

    async def read_agent(self, agent_id: str) -> Dict:
        """Read an agent."""
        async with self.session.get(  # type: ignore
            f"{self.base_url}/api/agents/{agent_id}",
            headers={"access-token": f"{self.api_key}"},
        ) as response:
            return await response.json()

    async def create_twitter_account(self, agent_id: str, account_data: Dict) -> Dict:
        """Create a Twitter account."""
        api_key = account_data.get("api_key", self.api_key)
        async with self.session.post(  # type: ignore
            f"{self.base_url}/api/agents/{agent_id}/twitter_accounts/",
            json=account_data,
            headers={"access-token": f"{api_key}"},
        ) as response:
            return await response.json()

    async def get_twitter_account(self, twitter_user_id: str) -> Dict:
        """Get a Twitter account."""
        async with self.session.get(  # type: ignore
            f"{self.base_url}/api/twitter_accounts/{twitter_user_id}",
            headers={"access-token": f"{self.api_key}"},
        ) as response:
            return await response.json()

    async def create_tweet(
        self, agent_id: int, twitter_user_id: str, tweet_data: Dict
    ) -> Dict:
        """Create a tweet."""
        async with self.session.post(  # type: ignore
            f"{self.base_url}/api/agents/{agent_id}/accounts/{twitter_user_id}/tweets/",
            json=tweet_data,
            headers={"access-token": f"{self.api_key}"},
        ) as response:
            return await response.json()

    async def read_tweet(self, tweet_id: str) -> Dict:
        """Read a tweet."""
        async with self.session.get(  # type: ignore
            f"{self.base_url}/api/tweets/{tweet_id}",
            headers={"access-token": f"{self.api_key}"},
        ) as response:
            return await response.json()

    async def create_interaction(
        self, agent_id: int, twitter_user_id: str, interaction_data: Dict
    ) -> Dict:
        """Create an interaction."""
        async with self.session.post(  # type: ignore
            f"{self.base_url}/api/agents/{agent_id}/accounts/{twitter_user_id}/interactions/",
            json=interaction_data,
            headers={"access-token": f"{self.api_key}"},
        ) as response:
            return await response.json()
