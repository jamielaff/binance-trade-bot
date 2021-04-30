import argparse
import json
import os
import re
import sys
import time
from decimal import Decimal
from typing import Dict, Optional

import requests
from unidecode import unidecode

from binance_client import Binance, MarginType
from twitter_utils import create_headers, reset_twitter_subscription_rules
from utils import log
from .logger import Logger


class ElonBot:
    def __init__(self, user: str,
                 config: Config,
                 dry_run: bool,
                 process_tweet_text: Optional[str]):

        self.logger = Logger()
        if config.TWITTER_API_KEY is None:
          self.logger.info("No Twitter API key, will not scan for Elons tweets")
          return None

        self.user = config.ELON_TWITTER_USERNAME
        self.crypto_rules = config.ELON_CRYPTO_RULES
        self.bridge = config.BRIDGE
        self.auto_buy_delay = config.ELON_AUTO_BUY_DELAY_SECONDS
        self.auto_sell_delay = config.ELON_AUTO_SELL_DELAY_SECONDS
        self.margin_type = config.ELON_MARGIN_TYPE
        self.order_size = config.ELON_ORDER_SIZE_MAX

        if config.GOOGLE_APPLICATION_CREDENTIALS is None:
          self.logger.info("No google app credentials, will not scan images")
          self.use_image_signal = false
        else
          self.use_image_signal = true

        self.process_tweet_text = config.ELON_TWEET_TEXT
        self.dry_run = false
        if self.process_tweet_text is not None:
            self.dry_run = true

        self.logger.info("Initialized ElonBot:")
        self.logger.info('  Twitter username:', self.user)
        self.logger.info('  Crypto rules:', self.crypto_rules)
        self.logger.info('  Sell back to (BRIDGE):', self.bridge)
        self.logger.info('  Auto buy time:', self.auto_buy_delay)
        self.logger.info('  Auto sell time:', self.auto_sell_delay)
        self.logger.info('  Use image signal:', self.use_image_signal)
        self.logger.info('  Margin type:', self.margin_type)
        self.logger.info('  Order size:', self.order_size)
        if self.dry_run:
            self.logger.info('  Dry run')
            self.logger.info('  Tweet text to process: ', self.process_tweet_text)
        self.logger.info('If running as a 2nd instance, please please please make sure BRIDGE and Crypto Rules do NOT exist in other instance!')
        self.logger.info('Bot will resume in 10 seconds...')
        time.sleep(10)

    def info(self):
        return {
            "twitter username": self.user,
            "crypto rules": self.crypto_rules,
            "sell back to": self.bridge,
            "auto buy delay": self.auto_buy_delay,
            "auto sell delay": self.auto_sell_delay,
            "use image signal": self.use_image_signal,
            "margin type": self.margin_type,
            "order size": self.order_size,
        }

    @staticmethod
    def get_image_text(uri: str) -> str:
        """Detects text in the file located in Google Cloud Storage or on the Web.
        """
        if uri is None or uri == '':
            return ''
        from google.cloud import vision
        try:
            client = vision.ImageAnnotatorClient()
            image = vision.Image()
            image.source.image_uri = uri
            response = client.text_detection(image=image)
            if response.error.message:
                self.logger.info('{}\nFor more info on error messages, check: '
                    'https://cloud.google.com/apis/design/errors'.format(response.error.message))
                return ''
            texts = response.text_annotations
            result = ' '.join([text.description for text in texts])
            self.logger.info('Extracted from the image:', result)
            return result
        except Exception as ex:
            self.logger.info('Failed to process attached image', ex)
            return ''

    def buy(self, coin: Coin, ticker: str):
        ask_price = self.manager.get_ask_price(ticker, coin.symbol)
        available_cash, _ = self.manager.get_available_asset(coin.symbol, ticker)
        if available_cash == 0:
            self.logger.info(f'Failed to buy {ticker}, no {coin.symbol} available')
            return None
        borrowable_cash = self.manager.get_max_borrowable(coin.symbol, ticker)
        if self.order_size == 'max':
            total_cash = available_cash + borrowable_cash
        else:
            max_cash = (available_cash + borrowable_cash) / available_cash
            if float(self.order_size) > max_cash:
                raise ValueError(f"Order size exceeds max margin: {self.order_size} > {max_cash}")
            total_cash = available_cash * Decimal(self.order_size)
        ticker_amount = total_cash / ask_price
        return self.manager.buy(ticker_amount, ticker, coin.symbol)

    def sell(self, ticker: str):
        _, available_ticker = self.manager.get_available_asset(self.bridge, ticker)
        return self.manager.sell(available_ticker, ticker, self.bridge)

    def trade(self, coin: Coin, ticker: str):
        time.sleep(self.auto_buy_delay)
        buy_result = self.buy(ticker, coin)
        if buy_result is None:
            return None
        self.logger.info('Waiting ', self.auto_sell_delay, ' seconds for before sell')
        time.sleep(self.auto_sell_delay)
        sell_result = self.sell(ticker)
        return buy_result, sell_result

    def tweet_has_crypto(self, tweet_json: str, current_coin):
        tweet_json = json.loads(tweet_json)
        self.logger.info("Tweet received\n", json.dumps(tweet_json, indent=4, sort_keys=True), "\n")
        tweet_text = tweet_json['data']['text']
        image_url = (tweet_json.get('includes', {}).get('media', [])[0:1] or [{}])[0].get('url', '')
        image_text = ''
        if self.use_image_signal:
            image_text = ElonBot.get_image_text(image_url)
        full_text = f'{tweet_text} {image_text}'
        for re_pattern, ticker in self.crypto_rules.items():
            t = unidecode(full_text)
            if re.search(re_pattern, t, flags=re.I) is not None:
                self.logger.info(f'Tweet matched pattern "{re_pattern}", will buy corresponding ticker: {ticker}')
                return ticker
        return None

    def has_crypto_tweet(self, timeout: int = 24 * 3600) -> None:
        if self.process_tweet_text is not None:
            return self.tweet_has_crypto(self.process_tweet_text, current_coin)
        reset_twitter_subscription_rules(self.user)
        while True:
            try:
                params = {'expansions': 'attachments.media_keys',
                          'media.fields': 'preview_image_url,media_key,url',
                          'tweet.fields': 'attachments,entities'}
                response = requests.get(
                    "https://api.twitter.com/2/tweets/search/stream",
                    headers=create_headers(), params=params, stream=True, timeout=timeout
                )
                self.logger.info('Subscribing to twitter updates. HTTP status:', response.status_code)
                if response.status_code != 200:
                    self.logger.error("Cannot get stream (HTTP {}): {}".format(response.status_code, response.text))
                    self.logger.info('Sleeping for 1 minute and retrying')
                    time.sleep(60)
                for response_line in response.iter_lines():
                    if response_line:
                        ticker = self.tweet_has_crypto(response_line)
                        if ticker is not None
                            return ticker
                        continue
                return None: