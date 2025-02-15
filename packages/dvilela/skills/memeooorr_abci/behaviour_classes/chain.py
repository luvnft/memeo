# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2024 David Vilela Freire
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

"""This package contains round behaviours of MemeooorrAbciApp."""

import json
from abc import ABC
from typing import Generator, Optional, Type, cast

from packages.dvilela.contracts.meme_factory.contract import MemeFactoryContract
from packages.dvilela.skills.memeooorr_abci.behaviour_classes.base import (
    MemeooorrBaseBehaviour,
)
from packages.dvilela.skills.memeooorr_abci.rounds import (
    ActionPreparationPayload,
    ActionPreparationRound,
    CheckFundsPayload,
    CheckFundsRound,
    Event,
    PullMemesPayload,
    PullMemesRound,
)
from packages.valory.contracts.gnosis_safe.contract import GnosisSafeContract
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.base import AbstractRound
from packages.valory.skills.transaction_settlement_abci.payload_tools import (
    hash_payload_to_hex,
)
from packages.valory.skills.transaction_settlement_abci.rounds import TX_HASH_LENGTH


EMPTY_CALL_DATA = b"0x"
SAFE_GAS = 0
ZERO_VALUE = 0
TWO_MINUTES = 120
SUMMON_BLOCK_DELTA = 100000


class ChainBehaviour(MemeooorrBaseBehaviour, ABC):  # pylint: disable=too-many-ancestors
    """ChainBehaviour"""

    def _build_safe_tx_hash(
        self,
        to_address: str,
        value: int = ZERO_VALUE,
        data: bytes = EMPTY_CALL_DATA,
    ) -> Generator[None, None, Optional[str]]:
        """Prepares and returns the safe tx hash for a tx."""

        self.context.logger.info(
            f"Preparing Safe transaction [{self.synchronized_data.safe_contract_address}] value={value}"
        )

        # Prepare the safe transaction
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.synchronized_data.safe_contract_address,
            contract_id=str(GnosisSafeContract.contract_id),
            contract_callable="get_raw_safe_transaction_hash",
            to_address=to_address,
            value=value,
            data=data,
            safe_tx_gas=SAFE_GAS,
            chain_id=self.get_chain_id(),
        )

        # Check for errors
        if response_msg.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.error(
                "Couldn't get safe tx hash. Expected response performative "
                f"{ContractApiMessage.Performative.STATE.value!r}, "  # type: ignore
                f"received {response_msg.performative.value!r}: {response_msg}."
            )
            return None

        # Extract the hash and check it has the correct length
        tx_hash: Optional[str] = cast(str, response_msg.state.body.get("tx_hash", None))

        if tx_hash is None or len(tx_hash) != TX_HASH_LENGTH:
            self.context.logger.error(
                "Something went wrong while trying to get the safe transaction hash. "
                f"Invalid hash {tx_hash!r} was returned."
            )
            return None

        # Transaction to hex
        tx_hash = tx_hash[2:]  # strip the 0x

        safe_tx_hash = hash_payload_to_hex(
            safe_tx_hash=tx_hash,
            ether_value=value,
            safe_tx_gas=SAFE_GAS,
            to_address=to_address,
            data=data,
        )

        self.context.logger.info(f"Safe transaction hash is {safe_tx_hash}")

        return safe_tx_hash

    def store_heart(self, token_nonce: int) -> Generator[None, None, None]:
        """Store a new hearted token to the db"""
        # Load previously hearted memes
        db_data = yield from self._read_kv(keys=("hearted_memes",))

        if db_data is None:
            self.context.logger.error("Error while loading the database")
            hearted_memes = []
        else:
            hearted_memes = json.loads(db_data["hearted_memes"] or "[]")

        # Write the new hearted token
        hearted_memes.append(token_nonce)
        yield from self._write_kv(
            {"hearted_memes": json.dumps(hearted_memes, sort_keys=True)}
        )
        self.context.logger.info("Wrote latest hearted token to db")


class CheckFundsBehaviour(ChainBehaviour):  # pylint: disable=too-many-ancestors
    """CheckFundsBehaviour"""

    matching_round: Type[AbstractRound] = CheckFundsRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            event = yield from self.get_event()

            payload = CheckFundsPayload(
                sender=self.context.agent_address,
                event=event,
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_event(self) -> Generator[None, None, str]:
        """Get the next event"""

        # Gas check
        native_balances = yield from self.get_native_balance()
        agent_native_balance = native_balances["agent"]

        if not agent_native_balance:
            return Event.NO_FUNDS.value

        if agent_native_balance < self.params.minimum_gas_balance:
            self.context.logger.info(
                f"Agent has insufficient funds for gas: {agent_native_balance} < {self.params.minimum_gas_balance}"
            )
            return Event.NO_FUNDS.value

        return Event.DONE.value


class PullMemesBehaviour(ChainBehaviour):  # pylint: disable=too-many-ancestors
    """PullMemesBehaviour"""

    matching_round: Type[AbstractRound] = PullMemesRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            meme_coins = yield from self.get_meme_coins()
            self.context.logger.info(f"Meme token list: {meme_coins}")

            payload = PullMemesPayload(
                sender=self.context.agent_address,
                meme_coins=json.dumps(meme_coins, sort_keys=True),
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_block_number(self) -> Generator[None, None, Optional[int]]:
        """Get the block number"""

        # Call the ledger connection (equivalent to web3.py)
        ledger_api_response = yield from self.get_ledger_api_response(
            performative=LedgerApiMessage.Performative.GET_STATE,
            ledger_callable="get_block_number",
            chain_id=self.get_chain_id(),
        )

        # Check for errors on the response
        if ledger_api_response.performative != LedgerApiMessage.Performative.STATE:
            self.context.logger.error(
                f"Error while retrieving block number: {ledger_api_response}"
            )
            return None

        # Extract and return the block number
        block_number = cast(
            int, ledger_api_response.state.body["get_block_number_result"]
        )

        self.context.logger.error(f"Got block number: {block_number}")

        return block_number


class ActionPreparationBehaviour(ChainBehaviour):  # pylint: disable=too-many-ancestors
    """ActionPreparationBehaviour"""

    matching_round: Type[AbstractRound] = ActionPreparationRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            tx_hash = yield from self.get_tx_hash()

            payload = ActionPreparationPayload(
                sender=self.context.agent_address,
                tx_hash=tx_hash,
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_tx_hash(self) -> Generator[None, None, Optional[str]]:
        """Get the action transaction hash"""

        token_action = self.synchronized_data.token_action

        # Action finished if we already have a final_tx_hash at this point
        if self.synchronized_data.final_tx_hash is not None:
            yield from self.post_action()
            return ""

        if not token_action:
            return None

        action = token_action["action"]

        contract_callable = f"build_{action}_tx"

        kwargs = {}

        if action in ["summon"]:
            kwargs["token_name"] = token_action["token_name"]
            kwargs["token_ticker"] = token_action["token_ticker"]
            kwargs["token_supply"] = int(token_action["token_supply"])

        if action in ["heart", "unleash"]:
            kwargs["meme_nonce"] = token_action["token_nonce"]

        if action in ["collect", "purge"]:
            kwargs["meme_address"] = token_action["token_address"]

        self.context.logger.info(f"Preparing the {action} transaction: kwargs={kwargs}")

        # Use the contract api to interact with the factory contract
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,  # type: ignore
            contract_address=self.get_meme_factory_address(),
            contract_id=str(MemeFactoryContract.contract_id),
            contract_callable=contract_callable,
            chain_id=self.get_chain_id(),
            **kwargs,
        )

        # Check that the response is what we expect
        if response_msg.performative != ContractApiMessage.Performative.RAW_TRANSACTION:
            self.context.logger.error(
                f"Error while building the {action} tx: {response_msg}"
            )
            return None

        data_bytes: Optional[bytes] = cast(
            bytes, response_msg.raw_transaction.body.get("data", None)
        )

        # Ensure that the data is not None
        if data_bytes is None:
            self.context.logger.error(
                f"Error while preparing the transaction: {response_msg}"
            )
            return None

        data_hex = data_bytes.hex()
        self.context.logger.info(f"Tx data is {data_hex}")

        # Check for errors
        if data_hex is None:
            return None

        # Prepare safe transaction
        value = (
            ZERO_VALUE
            if action not in ["summon", "heart"]
            else int(token_action["amount"])
        )  # to wei
        safe_tx_hash = yield from self._build_safe_tx_hash(
            to_address=self.get_meme_factory_address(),
            data=bytes.fromhex(data_hex),
            value=value,
        )

        # Optimistic design: we now store the hearted token address
        # Ideally, this should be done after a succesful heart transaction
        if action == "heart":
            self.store_heart(token_action["token_nonce"])

        return safe_tx_hash

    def post_action(  # pylint: disable=too-many-locals
        self,
    ) -> Generator[None, None, None]:
        """Post action"""
        token_action = self.synchronized_data.token_action
        token_nonce = yield from self.get_token_nonce()

        self.context.logger.info(f"The {token_action['action']} has finished")

        if not token_nonce:
            self.context.logger.error("Token nonce is none")
            return

        if token_action == "summon":  # nosec
            # Read previous tokens from db
            db_data = yield from self._read_kv(keys=("tokens",))

            if db_data is None:
                self.context.logger.error(
                    "Error while loading tokens from the database"
                )
                tokens = []
            else:
                tokens = json.loads(db_data["tokens"]) if db_data["tokens"] else []

            # Write token to db
            token_action = self.synchronized_data.token_action
            token_data = {
                "token_name": token_action["token_name"],
                "token_ticker": token_action["token_ticker"],
                "total_supply": int(token_action["total_supply"]),
                "token_nonce": token_nonce,
            }
            tokens.append(token_data)
            yield from self._write_kv(
                {"summoned_tokens": json.dumps(tokens, sort_keys=True)}
            )
            self.context.logger.info("Wrote latest token to db")

        if token_action in ["summon", "heart"]:
            self.store_heart(token_nonce)
            self.context.logger.info("Stored hearted token")

    def get_token_nonce(
        self,
    ) -> Generator[None, None, Optional[int]]:
        """Get the data from the deployment event"""

        # Use the contract api to interact with the factory contract
        response_msg = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.get_meme_factory_address(),
            contract_id=str(MemeFactoryContract.contract_id),
            contract_callable="get_token_data",
            tx_hash=self.synchronized_data.final_tx_hash,
            chain_id=self.get_chain_id(),
        )

        # Check that the response is what we expect
        if response_msg.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.error(f"Could not get the token data: {response_msg}")
            return None

        token_nonce = cast(int, response_msg.state.body.get("token_nonce", None))
        self.context.logger.info(f"Token nonce is {token_nonce}")
        return token_nonce
