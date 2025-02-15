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
import re
import secrets
from typing import Dict, Generator, List, Optional, Tuple, Type, Union

from twitter_text import parse_tweet  # type: ignore

from packages.dvilela.skills.memeooorr_abci.behaviour_classes.base import (
    MemeooorrBaseBehaviour,
)
from packages.dvilela.skills.memeooorr_abci.prompts import TWITTER_DECISION_PROMPT
from packages.dvilela.skills.memeooorr_abci.rounds import (
    ActionTweetPayload,
    ActionTweetRound,
    CollectFeedbackPayload,
    CollectFeedbackRound,
    EngageTwitterPayload,
    EngageTwitterRound,
    Event,
)
from packages.valory.skills.abstract_round_abci.base import AbstractRound


MAX_TWEET_CHARS = 280
JSON_RESPONSE_REGEXES = [r"json.?({.*})", r"json({.*})", r"\`\`\`json(.*)\`\`\`"]
MAX_TWEET_PREPARATIONS_RETRIES = 3


def parse_json_from_llm(response: str) -> Optional[Union[Dict, List]]:
    """Parse JSON from LLM response"""
    for JSON_RESPONSE_REGEX in JSON_RESPONSE_REGEXES:
        match = re.search(JSON_RESPONSE_REGEX, response, re.DOTALL)
        if not match:
            continue

        try:
            loaded_response = json.loads(match.groups()[0])
            return loaded_response
        except json.JSONDecodeError:
            continue
    return None


def is_tweet_valid(tweet: str) -> bool:
    """Checks a tweet length"""
    return parse_tweet(tweet).asdict()["weightedLength"] <= MAX_TWEET_CHARS


class BaseTweetBehaviour(MemeooorrBaseBehaviour):  # pylint: disable=too-many-ancestors
    """BaseTweetBehaviour"""

    matching_round: Type[AbstractRound] = None  # type: ignore

    def store_tweet(
        self, tweet: Union[dict, List[dict]]
    ) -> Generator[None, None, bool]:
        """Store tweet"""
        tweets = yield from self.get_tweets_from_db()
        if isinstance(tweet, list):
            tweets.extend(tweet)
        else:
            tweets.append(tweet)
        yield from self._write_kv({"tweets": json.dumps(tweets)})
        return True

    def post_tweet(
        self, tweet: List[str], store: bool = True
    ) -> Generator[None, None, Optional[Dict]]:
        """Post a tweet"""
        self.context.logger.info(f"Posting tweet: {tweet}")

        # Post the tweet
        tweet_ids = yield from self._call_twikit(
            method="post",
            tweets=[{"text": t} for t in tweet],
        )

        if not tweet_ids:
            self.context.logger.error("Failed posting to Twitter.")
            return None

        latest_tweet = {
            "tweet_id": tweet_ids[0],
            "text": tweet,
            "timestamp": self.get_sync_timestamp(),
        }

        # Write latest tweet to the database
        if store:
            yield from self.store_tweet(latest_tweet)
            self.context.logger.info("Wrote latest tweet to db")

        return latest_tweet

    def respond_tweet(
        self,
        tweet_id: str,
        text: str,
        quote: bool = False,
        user_name: Optional[str] = None,
    ) -> Generator[None, None, bool]:
        """Like a tweet"""

        self.context.logger.info(f"Liking tweet with ID: {tweet_id}")
        tweet = {"text": text}
        if quote:
            tweet["attachment_url"] = f"https://x.com/{user_name}/status/{tweet_id}"
        else:
            tweet["reply_to"] = tweet_id
        tweet_ids = yield from self._call_twikit(
            method="post",
            tweets=[tweet],
        )
        return tweet_ids is not None and tweet_ids

    def like_tweet(self, tweet_id: str) -> Generator[None, None, bool]:
        """Like a tweet"""
        self.context.logger.info(f"Liking tweet with ID: {tweet_id}")

        response = yield from self._call_twikit(method="like_tweet", tweet_id=tweet_id)
        return response["success"]

    def retweet(self, tweet_id: str) -> Generator[None, None, bool]:
        """Reweet"""
        self.context.logger.info(f"Retweeting tweet with ID: {tweet_id}")

        response = yield from self._call_twikit(method="retweet", tweet_id=tweet_id)
        return response["success"]

    def follow_user(self, user_id: str) -> Generator[None, None, bool]:
        """Follow user"""
        self.context.logger.info(f"Following user with ID: {user_id}")

        response = yield from self._call_twikit(method="follow_user", user_id=user_id)
        return response["success"]

    def get_previous_tweets(
        self, twitter_handle: str, limit: int = 20
    ) -> Generator[None, None, Optional[str]]:
        """Get the latest tweets formatted as numbered list

        Args:
            twitter_handle (str): The Twitter handle to fetch tweets from
            limit (int, optional): Maximum number of tweets to return. Defaults to 20.

        Returns:
            Generator yielding Optional[str]: Numbered list of tweets or empty string
        """

        delay = secrets.randbelow(5)
        self.context.logger.info(f"Sleeping for {delay} seconds")
        yield from self.sleep(delay)

        self.context.logger.info(f"Getting latest {limit} tweets from {twitter_handle}")
        previous_tweets = yield from self._call_twikit(
            method="get_user_tweets",
            twitter_handle=twitter_handle,
        )

        if previous_tweets:
            numbered_tweets = [
                f"{i+1}. {tweet['text']}"
                for i, tweet in enumerate(previous_tweets[:limit])
            ]
            return "\n".join(numbered_tweets)

        return ""


class CollectFeedbackBehaviour(
    MemeooorrBaseBehaviour
):  # pylint: disable=too-many-ancestors
    """CollectFeedbackBehaviour"""

    matching_round: Type[AbstractRound] = CollectFeedbackRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            feedback = yield from self.get_feedback()

            payload = CollectFeedbackPayload(
                sender=self.context.agent_address,
                feedback=json.dumps(feedback, sort_keys=True),
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_feedback(self) -> Generator[None, None, Optional[List]]:
        """Get the responses"""

        # Search new replies
        tweets = yield from self.get_tweets_from_db()
        if not tweets:
            self.context.logger.error("No tweets yet")
            return []
        latest_tweet = tweets[-1]
        query = f"conversation_id:{latest_tweet['tweet_id']}"
        feedback = yield from self._call_twikit(method="search", query=query, count=100)

        if feedback is None:
            self.context.logger.error(
                "Could not retrieve any replies due to an API error"
            )
            return None

        if not feedback:
            self.context.logger.error("No tweets match the query")
            return []

        self.context.logger.info(f"Retrieved {len(feedback)} replies")

        # Sort tweets by popularity using a weighted sum (views + quotes + retweets)
        feedback = list(
            sorted(
                feedback,
                key=lambda t: int(t.get("view_count", 0) or 0)
                + 3 * int(t.get("retweet_count", 0) or 0)
                + 5 * int(t.get("quote_count", 0) or 0),
                reverse=True,
            )
        )

        # Keep only the most relevant tweet to avoid sending too many tokens to the LLM
        feedback = feedback[:10]

        return feedback


class EngageTwitterBehaviour(BaseTweetBehaviour):  # pylint: disable=too-many-ancestors
    """EngageTwitterBehaviour"""

    matching_round: Type[AbstractRound] = EngageTwitterRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            event = yield from self.get_event()

            payload = EngageTwitterPayload(
                sender=self.context.agent_address,
                event=event,
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_event(self) -> Generator[None, None, str]:
        """Get the next event"""

        if self.params.skip_engagement:
            self.context.logger.info("Skipping engagement on Twitter")
            return Event.DONE.value

        # Get other memeooorr handles
        agent_handles = yield from self.get_memeooorr_handles_from_subgraph()

        # Filter out suspended accounts
        agent_handles = yield from self._call_twikit(
            method="filter_suspended_users",
            user_names=agent_handles,
        )
        self.context.logger.info(f"Not suspended users: {agent_handles}")

        # Load previously responded tweets
        db_data = yield from self._read_kv(keys=("interacted_tweet_ids",))

        if db_data is None:
            self.context.logger.error("Error while loading the database")
            interacted_tweet_ids = []
        else:
            interacted_tweet_ids = json.loads(db_data["interacted_tweet_ids"] or "[]")

        # Get their latest tweet
        pending_tweets = {}
        for agent_handle in agent_handles:
            # By default only 1 tweet is retrieved (the latest one)
            latest_tweets = yield from self._call_twikit(
                method="get_user_tweets",
                twitter_handle=agent_handle,
            )
            if not latest_tweets:
                self.context.logger.info("Couldn't get any tweets")
                continue
            tweet_id = latest_tweets[0]["id"]

            # Only respond to not previously interacted tweets
            if int(tweet_id) in interacted_tweet_ids:
                self.context.logger.info("Tweet was already interacted with")
                continue

            pending_tweets[tweet_id] = {
                "text": latest_tweets[0]["text"],
                "user_name": latest_tweets[0]["user_name"],
            }

        # Build and post interactions
        event, new_interacted_tweet_ids = yield from self.interact_twitter(
            pending_tweets
        )

        if event == Event.DONE.value:
            interacted_tweet_ids.extend(new_interacted_tweet_ids)
            # Write latest responded tweets to the database
            yield from self._write_kv(
                {
                    "interacted_tweet_ids": json.dumps(
                        interacted_tweet_ids, sort_keys=True
                    )
                }
            )
            self.context.logger.info("Wrote latest tweet to db")

        return event

    def interact_twitter(  # pylint: disable=too-many-locals,too-many-statements
        self, pending_tweets: dict
    ) -> Generator[None, None, Tuple[str, List]]:
        """Decide whether to like a tweet based on the persona."""
        new_interacted_tweet_ids: List[str] = []
        persona = yield from self.get_persona()

        other_tweets = "\n\n".join(
            [
                f"tweet_id: {t_id}\ntweet_text: {t_data['text']}"
                for t_id, t_data in pending_tweets.items()
            ]
        )

        # Get at most your 5 latest tweets
        tweets = yield from self.get_tweets_from_db()
        tweets = tweets[-5:]

        if tweets:
            previous_tweets = "\n\n".join(
                [
                    f"tweet_id: {tweet['tweet_id']}\ntweet_text: {tweet['text']}\ntimestamp: {tweet['timestamp']}"
                    for tweet in tweets
                ]
            )
        else:
            previous_tweets = "No previous tweets"

        prompt = TWITTER_DECISION_PROMPT.format(
            persona=persona,
            previous_tweets=previous_tweets,
            other_tweets=other_tweets,
            time=self.get_sync_time_str(),
        )

        llm_response = yield from self._call_genai(prompt=prompt)
        self.context.logger.info(f"LLM response for twitter decision: {llm_response}")

        if llm_response is None:
            self.context.logger.error("Error getting a response from the LLM.")
            return Event.ERROR.value, new_interacted_tweet_ids

        json_response = parse_json_from_llm(llm_response)

        if not json_response:
            return Event.ERROR.value, new_interacted_tweet_ids

        for interaction in json_response:
            tweet_id = interaction.get("tweet_id", None)
            action = interaction.get("action", None)
            text = interaction.get("text", None)

            if action == "none":
                continue

            if action != "tweet" and str(tweet_id) not in pending_tweets.keys():
                continue

            # use yield from self.sleep(1) to simulate a delay use secrests to randomize the delay
            # adding random delay to avoid rate limiting
            delay = secrets.randbelow(5)
            self.context.logger.info(f"Sleeping for {delay} seconds")
            yield from self.sleep(delay)

            self.context.logger.info("Sending a new tweet")
            if action == "tweet":
                yield from self.post_tweet(tweet=[text], store=True)
                continue

            self.context.logger.info(f"Trying to {action} tweet {tweet_id}")
            user_name = pending_tweets[str(tweet_id)]["user_name"]

            if action == "like":
                liked = yield from self.like_tweet(tweet_id)
                if liked:
                    new_interacted_tweet_ids.append(tweet_id)
                continue

            if action == "follow":
                followed = yield from self.follow_user(tweet_id)
                if followed:
                    new_interacted_tweet_ids.append(tweet_id)
                continue

            if action == "retweet":
                retweeted = yield from self.retweet(tweet_id)
                if retweeted:
                    new_interacted_tweet_ids.append(tweet_id)
                continue

            if not is_tweet_valid(text):
                self.context.logger.error("The tweet is too long.")
                continue

            if action == "reply":
                responded = yield from self.respond_tweet(tweet_id, text)
                if responded:
                    new_interacted_tweet_ids.append(tweet_id)

            if action == "quote":
                quoted = yield from self.respond_tweet(
                    tweet_id, text, quote=True, user_name=user_name
                )
                if quoted:
                    new_interacted_tweet_ids.append(tweet_id)

        return Event.DONE.value, new_interacted_tweet_ids


class ActionTweetBehaviour(BaseTweetBehaviour):  # pylint: disable=too-many-ancestors
    """ActionTweetBehaviour"""

    matching_round: Type[AbstractRound] = ActionTweetRound

    def async_act(self) -> Generator:
        """Do the act, supporting asynchronous execution."""

        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            event = yield from self.get_event()

            payload = ActionTweetPayload(
                sender=self.context.agent_address,
                event=event,
            )

        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()

        self.set_done()

    def get_event(self) -> Generator[None, None, str]:
        """Get the next event"""
        pending_tweet = self.synchronized_data.token_action["tweet"]
        self.context.logger.info("Sending the action tweet...")
        latest_tweet = yield from self.post_tweet(tweet=[pending_tweet], store=False)
        return Event.DONE.value if latest_tweet else Event.ERROR.value
