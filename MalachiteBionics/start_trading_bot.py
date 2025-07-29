#!/usr/bin/env python3
"""
Website Trading Bot Launcher
Simple script to start the website trading bot
"""

import os
import sys
import asyncio
import logging
from website_trading_bot import WebsiteTradingBot

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('website_trading_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("bot_launcher")

def check_environment():
    """Check if required environment variables are set"""
    required_vars = ['DATABASE_URL']
    missing_vars = []
    
    for var in required_vars:
        if not os.environ.get(var):
            missing_vars.append(var)
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return False
    
    return True

async def main():
    """Main launcher function"""
    logger.info("=" * 50)
    logger.info("Website Trading Bot Launcher")
    logger.info("=" * 50)
    
    # Check environment
    if not check_environment():
        logger.error("Environment check failed. Please set required variables.")
        return 1
    
    # Create and start the bot
    try:
        bot = WebsiteTradingBot()
        logger.info("Starting Website Trading Bot...")
        await bot.start()
        return 0
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C)")
        return 0
    except Exception as e:
        logger.error(f"Bot failed to start: {e}")
        return 1

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except Exception as e:
        logger.error(f"Launcher failed: {e}")
        sys.exit(1)
