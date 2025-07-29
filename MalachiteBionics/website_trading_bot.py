"""
Website Trading Bot - Integrated with Railway PostgreSQL
Replaces Discord bot functionality with website alerts
Connects to your existing Flask app database
"""

import asyncio
import aiohttp
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import logging
from datetime import datetime, timedelta
import traceback
import json
from typing import List, Dict, Optional

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("website_trading_bot")

# Database configuration (same as your Flask app)
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

class WebsiteTradingBot:
    def __init__(self):
        self.db_connection = None
        self.running = False
        self.last_check = None
        
    async def connect_database(self):
        """Connect to Railway PostgreSQL database"""
        try:
            if not DATABASE_URL:
                logger.error("DATABASE_URL not found in environment variables")
                return False
                
            self.db_connection = psycopg2.connect(
                DATABASE_URL,
                cursor_factory=RealDictCursor,
                sslmode='require'
            )
            logger.info("Successfully connected to Railway PostgreSQL database")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            return False
    
    def get_active_subscriptions(self) -> List[Dict]:
        """Get all active user subscriptions with their 2 selected coins"""
        try:
            with self.db_connection.cursor() as cursor:
                query = """
                    SELECT 
                        u.id as user_id,
                        u.email,
                        u.display_name,
                        s.plan_type,
                        s.coins,
                        s.status
                    FROM users u
                    INNER JOIN subscription s ON u.id = s.user_id
                    WHERE s.status = 'active' AND u.is_active = true
                """
                cursor.execute(query)
                subscriptions = cursor.fetchall()
                
                result = []
                for sub in subscriptions:
                    try:
                        # Parse the coins JSON
                        coins_list = json.loads(sub['coins']) if sub['coins'] else []
                        if len(coins_list) <= 2:  # Respect 2-coin limitation
                            result.append({
                                'user_id': sub['user_id'],
                                'email': sub['email'],
                                'display_name': sub['display_name'],
                                'plan_type': sub['plan_type'],
                                'coins': coins_list
                            })
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid coins JSON for user {sub['user_id']}")
                        continue
                
                logger.info(f"Found {len(result)} active subscriptions")
                return result
                
        except Exception as e:
            logger.error(f"Error getting active subscriptions: {e}")
            return []
    
    def create_trading_alert(self, user_id: int, coin_pair: str, alert_type: str, 
                           price: float, confidence: int, algorithm: str, message: str):
        """Create a trading alert in the database (replaces Discord message)"""
        try:
            with self.db_connection.cursor() as cursor:
                query = """
                    INSERT INTO trading_alert 
                    (user_id, coin_pair, alert_type, price, confidence, algorithm, message, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """
                expires_at = datetime.utcnow() + timedelta(hours=24)
                
                cursor.execute(query, (
                    user_id, coin_pair, alert_type, price, confidence, 
                    algorithm, message, expires_at
                ))
                self.db_connection.commit()
                
                logger.info(f"Created {alert_type} alert for user {user_id}: {coin_pair}")
                return True
                
        except Exception as e:
            logger.error(f"Error creating trading alert: {e}")
            self.db_connection.rollback()
            return False

    async def fetch_klines(self, symbol: str, interval: str = "5m", limit: int = 100) -> Optional[pd.DataFrame]:
        """Fetch kline data from Binance API (same as your bot)"""
        url = f"https://api.binance.com/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        
        max_retries = 3
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.get(url, params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if not data:
                                logger.warning(f"Empty data received for {symbol}")
                                return None
                            
                            df = pd.DataFrame([{
                                "timestamp": int(d[0]),
                                "open": float(d[1]),
                                "high": float(d[2]),
                                "low": float(d[3]),
                                "close": float(d[4]),
                                "volume": float(d[5])
                            } for d in data])
                            
                            return df
                        else:
                            logger.warning(f"HTTP {resp.status} for {symbol}, attempt {attempt + 1}")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(retry_delay * (attempt + 1))
                            
            except asyncio.TimeoutError:
                logger.warning(f"Timeout fetching {symbol}, attempt {attempt + 1}")
            except Exception as e:
                logger.error(f"Error fetching {symbol}, attempt {attempt + 1}: {e}")
                
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (attempt + 1))
        
        logger.error(f"Failed to fetch data for {symbol} after {max_retries} attempts")
        return None

    def calculate_rsi(self, closes: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI (same as your bot)"""
        try:
            if len(closes) < period + 1:
                logger.warning(f"Insufficient data for RSI calculation: {len(closes)} < {period + 1}")
                return pd.Series([50] * len(closes), index=closes.index)
            
            delta = closes.diff()
            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)
            
            avg_gain = gain.rolling(window=period, min_periods=period).mean()
            avg_loss = loss.rolling(window=period, min_periods=period).mean()
            
            # Avoid division by zero
            avg_loss = avg_loss.replace(0, 1e-8)
            
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            
            return rsi
        except Exception as e:
            logger.error(f"Error calculating RSI: {e}")
            return pd.Series([50] * len(closes), index=closes.index)

    def calculate_macd(self, series: pd.Series, short_span: int = 12, long_span: int = 26, signal_span: int = 9):
        """Calculate MACD (same as your bot)"""
        try:
            if len(series) < long_span:
                logger.warning(f"Insufficient data for MACD calculation: {len(series)} < {long_span}")
                return pd.Series([0] * len(series), index=series.index), \
                       pd.Series([0] * len(series), index=series.index), \
                       pd.Series([0] * len(series), index=series.index)
            
            ema_short = series.ewm(span=short_span, adjust=False).mean()
            ema_long = series.ewm(span=long_span, adjust=False).mean()
            macd_line = ema_short - ema_long
            signal_line = macd_line.ewm(span=signal_span, adjust=False).mean()
            histogram = macd_line - signal_line
            
            return macd_line, signal_line, histogram
        except Exception as e:
            logger.error(f"Error calculating MACD: {e}")
            return pd.Series([0] * len(series), index=series.index), \
                   pd.Series([0] * len(series), index=series.index), \
                   pd.Series([0] * len(series), index=series.index)

    def calculate_momentum(self, series: pd.Series, period: int = 10) -> pd.Series:
        """Calculate momentum (same as your bot)"""
        try:
            if len(series) < period + 1:
                logger.warning(f"Insufficient data for momentum calculation: {len(series)} < {period + 1}")
                return pd.Series([0] * len(series), index=series.index)
            
            return series - series.shift(period)
        except Exception as e:
            logger.error(f"Error calculating momentum: {e}")
            return pd.Series([0] * len(series), index=series.index)

    def predict_v3(self, rsi_value: float) -> str:
        """V3 Trading Algorithm - RSI only (your original logic)"""
        try:
            rsi = float(rsi_value)
            
            if rsi <= 30:
                return "Buy"
            elif rsi >= 70:
                return "Sell"
            else:
                return "Neutral"
                
        except (ValueError, TypeError):
            return "Neutral"

    def predict_v6(self, rsi_value: float, macd_histogram: float) -> str:
        """V6 Trading Algorithm - RSI + MACD (your original logic)"""
        try:
            rsi = float(rsi_value)
            macd_hist = float(macd_histogram)
            
            # RSI oversold with positive MACD momentum
            if rsi <= 35 and macd_hist > 0:
                return "Buy"
            
            # RSI overbought with negative MACD momentum  
            elif rsi >= 65 and macd_hist < 0:
                return "Sell"
            
            # Strong momentum signals regardless of RSI
            elif rsi <= 25:  # Very oversold
                return "Buy"
            elif rsi >= 75:  # Very overbought
                return "Sell"
            
            else:
                return "Neutral"
                
        except (ValueError, TypeError):
            return "Neutral"

    def predict_v9(self, rsi_value: float, macd_histogram: float, momentum: float) -> str:
        """V9 Trading Algorithm - RSI + MACD + Momentum (your original logic)"""
        try:
            rsi = float(rsi_value)
            macd_hist = float(macd_histogram)
            mom = float(momentum)
            
            # Strong buy signals - all indicators align
            if rsi <= 30 and macd_hist > 0 and mom > 0:
                return "Buy"
            
            # Strong sell signals - all indicators align
            elif rsi >= 70 and macd_hist < 0 and mom < 0:
                return "Sell"
            
            # Medium strength signals - 2 out of 3 indicators
            elif rsi <= 35 and (macd_hist > 0 or mom > 0):
                return "Buy"
            elif rsi >= 65 and (macd_hist < 0 or mom < 0):
                return "Sell"
            
            # Very extreme RSI levels override other indicators
            elif rsi <= 20:
                return "Buy"
            elif rsi >= 80:
                return "Sell"
            
            # Strong momentum with moderate RSI
            elif mom > 5 and rsi < 50:
                return "Buy"
            elif mom < -5 and rsi > 50:
                return "Sell"
            
            else:
                return "Neutral"
                
        except (ValueError, TypeError):
            return "Neutral"

    def predict_elite(self, rsi_value: float, macd_histogram: float, momentum: float) -> str:
        """Elite Algorithm - Enhanced version for premium users"""
        try:
            rsi = float(rsi_value)
            macd_hist = float(macd_histogram)
            mom = float(momentum)
            
            # Elite algorithm with more sophisticated logic
            score = 0
            
            # RSI scoring
            if rsi <= 25:
                score += 3
            elif rsi <= 35:
                score += 2
            elif rsi >= 75:
                score -= 3
            elif rsi >= 65:
                score -= 2
            
            # MACD scoring
            if macd_hist > 0.001:
                score += 2
            elif macd_hist > 0:
                score += 1
            elif macd_hist < -0.001:
                score -= 2
            elif macd_hist < 0:
                score -= 1
            
            # Momentum scoring
            if mom > 10:
                score += 2
            elif mom > 2:
                score += 1
            elif mom < -10:
                score -= 2
            elif mom < -2:
                score -= 1
            
            # Elite decision logic
            if score >= 4:
                return "Buy"
            elif score <= -4:
                return "Sell"
            else:
                return "Neutral"
                
        except (ValueError, TypeError):
            return "Neutral"

    async def analyze_coin_for_user(self, user_data: Dict, coin: str):
        """Analyze a specific coin for a specific user"""
        try:
            symbol = f"{coin.upper()}USDT"
            user_id = user_data['user_id']
            plan_type = user_data['plan_type']
            
            # Fetch market data
            df = await self.fetch_klines(symbol)
            if df is None or df.empty:
                logger.warning(f"No data available for {symbol}")
                return

            if len(df) < 30:  # Minimum data requirement
                logger.warning(f"Insufficient data for {symbol}: {len(df)} rows")
                return

            # Calculate indicators
            df["rsi"] = self.calculate_rsi(df["close"])
            rsi_val = df["rsi"].iloc[-1]
            
            if pd.isna(rsi_val):
                logger.warning(f"RSI calculation failed for {symbol}")
                return

            current_price = df["close"].iloc[-1]
            
            # Calculate additional indicators based on plan
            macd_hist = None
            momentum = None
            confidence = 85  # Default confidence
            
            if plan_type in ["v6", "v9", "elite"]:
                _, _, macd_hist_series = self.calculate_macd(df["close"])
                macd_hist = macd_hist_series.iloc[-1]
                if pd.isna(macd_hist):
                    logger.warning(f"MACD calculation failed for {symbol}")
                    return
                    
            if plan_type in ["v9", "elite"]:
                momentum_series = self.calculate_momentum(df["close"])
                momentum = momentum_series.iloc[-1]
                if pd.isna(momentum):
                    logger.warning(f"Momentum calculation failed for {symbol}")
                    return

            # Get prediction based on user's plan
            signal = "Neutral"
            
            if plan_type == "v3":
                signal = self.predict_v3(rsi_val)
                confidence = 80
            elif plan_type == "v6":
                signal = self.predict_v6(rsi_val, macd_hist)
                confidence = 85
            elif plan_type == "v9":
                signal = self.predict_v9(rsi_val, macd_hist, momentum)
                confidence = 90
            elif plan_type == "elite":
                signal = self.predict_elite(rsi_val, macd_hist, momentum)
                confidence = 95

            # Create alert if signal is actionable
            if signal in ["Buy", "Sell"]:
                # Create detailed message
                message = f"{signal.upper()} signal for {symbol}\n"
                message += f"Algorithm: {plan_type.upper()}\n"
                message += f"RSI: {rsi_val:.2f}\n"
                
                if macd_hist is not None:
                    message += f"MACD Histogram: {macd_hist:.6f}\n"
                if momentum is not None:
                    message += f"Momentum: {momentum:.4f}\n"
                
                message += f"Price: ${current_price:.4f}\n"
                message += f"Confidence: {confidence}%\n"
                message += f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
                
                # Create the alert in database (replaces Discord message)
                success = self.create_trading_alert(
                    user_id=user_id,
                    coin_pair=f"{coin}/USD",
                    alert_type=signal.lower(),
                    price=current_price,
                    confidence=confidence,
                    algorithm=plan_type,
                    message=message
                )
                
                if success:
                    logger.info(f"Created {signal} alert for user {user_data['email']} - {symbol} ({plan_type})")
                else:
                    logger.error(f"Failed to create alert for user {user_id} - {symbol}")

        except Exception as e:
            logger.error(f"Error analyzing {coin} for user {user_data.get('email', 'unknown')}: {e}")
            traceback.print_exc()

    async def monitoring_loop(self):
        """Main monitoring loop - runs continuously"""
        logger.info("Starting website trading bot monitoring loop")
        
        while self.running:
            try:
                start_time = datetime.utcnow()
                
                # Get all active subscriptions
                subscriptions = self.get_active_subscriptions()
                
                if not subscriptions:
                    logger.info("No active subscriptions found, sleeping...")
                    await asyncio.sleep(420)  # 7 minutes
                    continue
                
                # Process each user's coins
                tasks = []
                for user_data in subscriptions:
                    coins = user_data.get('coins', [])
                    for coin in coins:
                        if coin and coin.strip():  # Make sure coin is valid
                            tasks.append(self.analyze_coin_for_user(user_data, coin.strip()))
                
                if tasks:
                    logger.info(f"Analyzing {len(tasks)} coin-user combinations")
                    await asyncio.gather(*tasks, return_exceptions=True)
                
                # Update tracking
                self.last_check = datetime.utcnow()
                processing_time = (self.last_check - start_time).total_seconds()
                
                logger.info(f"Monitoring cycle completed in {processing_time:.2f}s for {len(subscriptions)} users")
                
                # Wait 7 minutes before next cycle (same as your Discord bot)
                await asyncio.sleep(420)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                traceback.print_exc()
                await asyncio.sleep(60)  # Wait 1 minute before retrying

    async def start(self):
        """Start the website trading bot"""
        logger.info("Starting Website Trading Bot...")
        
        # Connect to database
        if not await self.connect_database():
            logger.error("Failed to connect to database. Exiting.")
            return False
        
        self.running = True
        
        # Start monitoring loop
        try:
            await self.monitoring_loop()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            traceback.print_exc()
        finally:
            self.running = False
            if self.db_connection:
                self.db_connection.close()
                logger.info("Database connection closed")
        
        return True

    def stop(self):
        """Stop the trading bot"""
        logger.info("Stopping Website Trading Bot...")
        self.running = False

async def main():
    """Main entry point"""
    bot = WebsiteTradingBot()
    await bot.start()

if __name__ == "__main__":
    try:
        # Run the website trading bot
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Website Trading Bot stopped by user")
    except Exception as e:
        logger.error(f"Failed to start Website Trading Bot: {e}")
        traceback.print_exc()
