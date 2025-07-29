"""
Background service to run the trading bot alongside your Flask app
This will start the bot as a background task when your Flask app starts
"""

import threading
import asyncio
import logging
from website_trading_bot import WebsiteTradingBot

logger = logging.getLogger("bot_service")

class TradingBotService:
    def __init__(self):
        self.bot = None
        self.thread = None
        self.running = False
    
    def start_bot_background(self):
        """Start the trading bot in a background thread"""
        if self.running:
            logger.info("Trading bot is already running")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run_bot_async, daemon=True)
        self.thread.start()
        logger.info("🤖 Trading bot service started in background")
    
    def _run_bot_async(self):
        """Run the async bot in a new event loop"""
        try:
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # Start the bot
            self.bot = WebsiteTradingBot()
            loop.run_until_complete(self.bot.start())
        except Exception as e:
            logger.error(f"Trading bot crashed: {e}")
        finally:
            self.running = False
    
    def stop_bot(self):
        """Stop the trading bot"""
        if self.bot:
            self.bot.stop()
        self.running = False
        logger.info("Trading bot service stopped")

# Global instance
trading_bot_service = TradingBotService()

def start_trading_bot():
    """Function to call from your Flask app"""
    trading_bot_service.start_bot_background()

def stop_trading_bot():
    """Function to call when Flask app shuts down"""
    trading_bot_service.stop_bot()
