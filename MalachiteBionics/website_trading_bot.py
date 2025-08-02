"""
Website Trading Bot - Integrated with Railway PostgreSQL
Replaces Discord bot functionality with website alerts
Connects to your existing Flask app database
"""

import asyncio
import aiohttp
import pandas as pd
import sqlite3
import os
import logging
from datetime import datetime, timedelta
import traceback
import json
from typing import List, Dict, Optional

# Try to import PostgreSQL support, but fallback to SQLite if not available
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

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
        """Connect to database (PostgreSQL or SQLite)"""
        try:
            # Check if we have a PostgreSQL DATABASE_URL and psycopg2 is available
            if DATABASE_URL and DATABASE_URL.startswith(('postgresql://', 'postgres://')) and POSTGRES_AVAILABLE:
                # Use PostgreSQL for production (Railway)
                self.db_connection = psycopg2.connect(
                    DATABASE_URL,
                    cursor_factory=RealDictCursor,
                    sslmode='require'
                )
                logger.info("Successfully connected to Railway PostgreSQL database")
                self.db_type = 'postgresql'
                return True
            else:
                # Use SQLite for local development
                db_path = os.path.join(os.path.dirname(__file__), 'trading_bot.db')
                self.db_connection = sqlite3.connect(db_path, check_same_thread=False)
                self.db_connection.row_factory = sqlite3.Row  # For dict-like access
                logger.info(f"Successfully connected to SQLite database: {db_path}")
                self.db_type = 'sqlite'
                return True
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            # Instead of returning False, let's disable the bot gracefully
            logger.info("Database connection failed - bot will run in no-database mode")
            self.db_connection = None
            self.db_type = None
            return False
    
    def execute_db_query(self, query: str, params: tuple = None, fetch_type: str = 'all'):
        """Execute database query with proper parameter substitution for SQLite or PostgreSQL"""
        if not self.db_connection:
            logger.warning("No database connection available - skipping query")
            return [] if fetch_type == 'all' else None
            
        try:
            # Convert PostgreSQL-style %s placeholders to SQLite-style ? placeholders
            if self.db_type == 'sqlite' and '%s' in query:
                query = query.replace('%s', '?')
            
            cursor = self.db_connection.cursor()
            
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            
            if fetch_type == 'all':
                result = cursor.fetchall()
            elif fetch_type == 'one':
                result = cursor.fetchone()
            else:
                result = None
                
            # Commit for INSERT/UPDATE/DELETE operations
            if query.strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
                self.db_connection.commit()
            
            cursor.close()
            return result
            
        except Exception as e:
            logger.error(f"Database query error: {e}")
            return [] if fetch_type == 'all' else None
    
    def get_active_subscriptions(self) -> List[Dict]:
        """Get all users with online bot status (admin + active subscribers)"""
        try:
            # Get all users with online bot status
            query = """
                SELECT 
                    u.id as user_id,
                    u.email,
                    u.display_name,
                    u.is_admin,
                    u.bot_status,
                    u.bot_last_active,
                    s.plan_type,
                    s.coins,
                    s.status as subscription_status
                FROM user u
                LEFT JOIN subscription s ON u.id = s.user_id AND s.status = 'active'
                WHERE u.bot_status = 'online' 
                AND u.is_active = 1
                AND (
                    u.is_admin = 1 OR 
                    (s.status = 'active' AND s.id IS NOT NULL)
                )
            """
            users = self.execute_db_query(query, fetch_type='all')
            
            result = []
            for user in users:
                try:
                    user_data = {
                        'user_id': user['user_id'] if isinstance(user, dict) else user[0],
                        'email': user['email'] if isinstance(user, dict) else user[1],
                        'display_name': user['display_name'] if isinstance(user, dict) else user[2],
                        'is_admin': user['is_admin'] if isinstance(user, dict) else user[3],
                        'bot_status': user['bot_status'] if isinstance(user, dict) else user[4],
                        'bot_last_active': user['bot_last_active'] if isinstance(user, dict) else user[5],
                        'plan_type': user['plan_type'] if isinstance(user, dict) else user[6],
                        'subscription_status': user['subscription_status'] if isinstance(user, dict) else user[8]
                    }
                    
                    # Handle coins parsing
                    coins_data = user['coins'] if isinstance(user, dict) else user[7]
                    
                    if user_data['is_admin']:
                        # Admin gets free version with default coins
                        user_data['plan_type'] = 'free'
                        user_data['coins'] = ['SOL', 'RAY']  # Default coins for admin
                    else:
                        # Regular customer with subscription
                        if coins_data:
                            try:
                                coins_list = json.loads(coins_data) if isinstance(coins_data, str) else coins_data
                                if len(coins_list) <= 2:  # Respect 2-coin limitation
                                    user_data['coins'] = coins_list
                                else:
                                    continue  # Skip if too many coins
                            except (json.JSONDecodeError, TypeError):
                                continue  # Skip if invalid coins data
                        else:
                            continue  # Skip if no coins selected
                    
                    if user_data.get('coins'):  # Only add if has coins to monitor
                        result.append(user_data)
                        
                except (json.JSONDecodeError, TypeError) as e:
                    user_id = user['user_id'] if isinstance(user, dict) else user[0]
                    logger.warning(f"Invalid coins JSON for user {user_id}: {e}")
                    continue
                
                logger.info(f"Found {len(result)} users with online bots ({sum(1 for u in result if u['is_admin'])} admin, {sum(1 for u in result if not u['is_admin'])} customers)")
                return result
                
        except Exception as e:
            logger.error(f"Error getting active subscriptions: {e}")
            return []
    
    def create_trading_alert(self, user_id: int, coin_pair: str, alert_type: str, 
                           price: float, confidence: int, algorithm: str, message: str):
        """Create a trading alert in the database and send push notification"""
        try:
            query = """
                INSERT INTO trading_alert 
                (user_id, coin_pair, alert_type, price, confidence, algorithm, message, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            expires_at = datetime.utcnow() + timedelta(hours=24)
            
            self.execute_db_query(query, (
                user_id, coin_pair, alert_type, price, confidence, 
                algorithm, message, expires_at
            ))
            
            logger.info(f"Created {alert_type} alert for user {user_id}: {coin_pair}")
            
            # Send push notification if user has enabled it
            self.send_push_notification(user_id, coin_pair, alert_type, price, confidence, algorithm)
            
            return True
                
        except Exception as e:
            logger.error(f"Error creating trading alert: {e}")
            if self.db_type == 'postgresql' and self.db_connection:
                self.db_connection.rollback()
            return False

    def send_push_notification(self, user_id: int, coin_pair: str, alert_type: str, 
                             price: float, confidence: int, algorithm: str):
        """Send push notification for trading alert"""
        try:
            # Get user's push notification subscription
            query = """
                SELECT email, push_notifications_enabled, push_subscription_endpoint,
                       push_subscription_p256dh, push_subscription_auth
                FROM user WHERE id = %s AND push_notifications_enabled = 1
            """
            
            user_data = self.execute_db_query(query, (user_id,), fetch_type='one')
            
            if not user_data:
                return  # User doesn't have push notifications enabled
            
            # Import push notification service
            from push_notifications import PushNotificationService
            push_service = PushNotificationService()
            
            # Create user object for push service
            class UserObj:
                def __init__(self, data):
                    if isinstance(data, dict):
                        self.email = data['email']
                        self.push_subscription_endpoint = data['push_subscription_endpoint']
                        self.push_subscription_p256dh = data['push_subscription_p256dh']
                        self.push_subscription_auth = data['push_subscription_auth']
                    else:  # SQLite row tuple
                        self.email = data[0]
                        self.push_subscription_endpoint = data[2]
                        self.push_subscription_p256dh = data[3]
                        self.push_subscription_auth = data[4]
            
            user_obj = UserObj(user_data)
            
            # Format price and change percentage
            price_str = f"${price:,.2f}" if price else "N/A"
            change_str = "N/A"  # You can calculate this from price data if needed
            
            # Send push notification
            result = push_service.send_trading_alert_notification(
                user=user_obj,
                symbol=coin_pair,
                price=price_str,
                change=change_str,
                algorithm=algorithm,
                alert_type=alert_type,
                confidence=f"{confidence}%"
            )
            
            if result:
                logger.info(f"Push notification sent to user {user_id} for {coin_pair}")
            else:
                logger.warning(f"Failed to send push notification to user {user_id}")
                    
        except Exception as e:
            logger.error(f"Error sending push notification: {e}")
            # Don't let push notification errors break the alert creation

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
        """Calculate RSI (your v12 algorithm)"""
        try:
            if len(closes) < period + 1:
                logger.warning(f"Insufficient data for RSI calculation: {len(closes)} < {period + 1}")
                return pd.Series([50] * len(closes), index=closes.index)
            
            delta = closes.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            return rsi.fillna(50)
        except Exception as e:
            logger.error(f"Error calculating RSI: {e}")
            return pd.Series([50] * len(closes), index=closes.index)

    def calculate_macd(self, series: pd.Series, short_span: int = 12, long_span: int = 26, signal_span: int = 9):
        """Calculate MACD (your v12 algorithm)"""
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

    def calculate_confidence(self, rsi_val: float, macd_val: float) -> float:
        """Calculate confidence score (your v12 algorithm)"""
        try:
            # Confidence based on how far RSI is from 50 and MACD histogram magnitude
            rsi_conf = abs(rsi_val - 50) / 50  # normalized 0 to 1
            macd_conf = min(abs(macd_val) * 10, 1)  # scale MACD and cap at 1
            combined_conf = (rsi_conf + macd_conf) / 2
            return combined_conf * 100  # scale to 0-100
        except Exception as e:
            logger.error(f"Error calculating confidence: {e}")
            return 50.0

    def update_user_bot_activity(self, user_id: int):
        """Update user's bot last active timestamp"""
        try:
            query = """
                UPDATE user 
                SET bot_last_active = %s 
                WHERE id = %s AND bot_status = 'online'
            """
            self.execute_db_query(query, (datetime.utcnow(), user_id))
            
        except Exception as e:
            logger.error(f"Error updating bot activity for user {user_id}: {e}")

    def predict_free(self, rsi_value: float, macd_histogram: float) -> str:
        """Free Algorithm - Admin's main_bot_code.py logic (RSI + MACD)"""
        try:
            rsi = float(rsi_value)
            macd_val = float(macd_histogram)
            
            # Your main_bot_code.py algorithm: RSI < 30 and MACD > 0 = Buy, RSI > 70 and MACD < 0 = Sell
            if rsi < 30 and macd_val > 0:
                return "Buy"
            elif rsi > 70 and macd_val < 0:
                return "Sell"
            else:
                return "Neutral"
                
        except (ValueError, TypeError):
            return "Neutral"

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

    def predict_v12(self, rsi_value: float, macd_histogram: float) -> str:
        """V12 Trading Algorithm - Your latest algorithm with RSI + MACD"""
        try:
            rsi = float(rsi_value)
            macd_val = float(macd_histogram)
            
            # Your v12 algorithm logic: RSI < 30 and MACD > 0 = Buy, RSI > 70 and MACD < 0 = Sell
            if rsi < 30 and macd_val > 0:
                return "Buy"
            elif rsi > 70 and macd_val < 0:
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
            is_admin = user_data.get('is_admin', False)
            
            # Update user's bot activity
            self.update_user_bot_activity(user_id)
            
            # Fetch market data (using 1h intervals like your v12 bot)
            df = await self.fetch_klines(symbol, interval="1h", limit=100)
            if df is None or df.empty:
                logger.warning(f"No data available for {symbol}")
                return

            if len(df) < 30:  # Minimum data requirement
                logger.warning(f"Insufficient data for {symbol}: {len(df)} rows")
                return

            # Calculate indicators
            df["rsi"] = self.calculate_rsi(df["close"])
            _, _, macd_histogram = self.calculate_macd(df["close"])
            
            rsi_val = df["rsi"].iloc[-1]
            macd_val = macd_histogram.iloc[-1]
            
            if pd.isna(rsi_val) or pd.isna(macd_val):
                logger.warning(f"Indicator calculation failed for {symbol}")
                return

            current_price = df["close"].iloc[-1]
            
            # Calculate confidence
            confidence = self.calculate_confidence(rsi_val, macd_val)
            
            # Get prediction based on user's plan
            signal = "Neutral"
            
            if plan_type == "free":  # Admin gets free version
                signal = self.predict_free(rsi_val, macd_val)
                confidence = 80  # Free version confidence
            elif plan_type in ["basic", "v3"]:  # Basic = V3
                signal = self.predict_v3(rsi_val)
                confidence = 80
            elif plan_type in ["classic", "v6"]:  # Classic = V6
                signal = self.predict_v6(rsi_val, macd_val)
                confidence = 85
            elif plan_type in ["advanced", "v9"]:  # Advanced = V9
                momentum_series = self.calculate_momentum(df["close"])
                momentum_val = momentum_series.iloc[-1]
                if not pd.isna(momentum_val):
                    signal = self.predict_v9(rsi_val, macd_val, momentum_val)
                    confidence = 90
                else:
                    signal = self.predict_v12(rsi_val, macd_val)  # Fallback to v12
                    confidence = 90
            elif plan_type in ["premium", "elite", "v12"]:  # Premium/Elite = V12
                signal = self.predict_v12(rsi_val, macd_val)  # Premium & Elite use v12 algorithm
                confidence = 95
            else:
                # Default to v12 algorithm for unknown plans
                signal = self.predict_v12(rsi_val, macd_val)
                confidence = 85

            # Log the analysis
            user_type = "ADMIN" if is_admin else "CUSTOMER"
            logger.info(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} - {user_type} {user_data['email']} - {coin.upper()} RSI: {rsi_val:.2f}, MACD: {macd_val:.4f}")

            # Create alert if signal is actionable (not Neutral)
            if signal in ["Buy", "Sell"]:
                # Create detailed message
                message = f"{signal.upper()} signal for {symbol}\n"
                message += f"Algorithm: {plan_type.upper()}\n"
                message += f"RSI: {rsi_val:.2f}\n"
                message += f"MACD Histogram: {macd_val:.4f}\n"
                message += f"Confidence Score: {confidence:.1f}/100\n"
                message += f"Price: ${current_price:.4f}\n"
                
                if is_admin:
                    message += f"Account: ADMIN (Free Version)\n"
                else:
                    message += f"Account: Customer ({plan_type.upper()})\n"
                    
                message += f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
                
                # Create the alert in database
                success = self.create_trading_alert(
                    user_id=user_id,
                    coin_pair=f"{coin.upper()}/USD",
                    alert_type=signal.lower(),
                    price=current_price,
                    confidence=int(confidence),
                    algorithm=plan_type,
                    message=message
                )
                
                if success:
                    user_label = f"ADMIN {user_data['email']}" if is_admin else f"CUSTOMER {user_data['email']}"
                    logger.info(f"Created {signal} alert for {user_label} - {symbol} ({plan_type}) - Confidence: {confidence:.1f}%")
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
                
                # Get all users with online bot status
                active_users = self.get_active_subscriptions()
                
                if not active_users:
                    logger.info("No users with online bots found, sleeping...")
                    await asyncio.sleep(420)  # 7 minutes
                    continue
                
                # Process each user's coins
                tasks = []
                admin_count = 0
                customer_count = 0
                
                for user_data in active_users:
                    if user_data.get('is_admin'):
                        admin_count += 1
                    else:
                        customer_count += 1
                        
                    coins = user_data.get('coins', [])
                    for coin in coins:
                        if coin and coin.strip():  # Make sure coin is valid
                            tasks.append(self.analyze_coin_for_user(user_data, coin.strip()))
                
                if tasks:
                    logger.info(f"Analyzing {len(tasks)} coin-user combinations ({admin_count} admin bots, {customer_count} customer bots)")
                    await asyncio.gather(*tasks, return_exceptions=True)
                
                # Update tracking
                self.last_check = datetime.utcnow()
                processing_time = (self.last_check - start_time).total_seconds()
                
                logger.info(f"Monitoring cycle completed in {processing_time:.2f}s for {len(active_users)} users")
                
                # Wait 7 minutes before next cycle (same as your Discord bot)
                await asyncio.sleep(420)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                traceback.print_exc()
                await asyncio.sleep(60)  # Wait 1 minute before retrying

    async def start(self):
        """Start the website trading bot"""
        logger.info("Starting Website Trading Bot...")
        
        # Connect to database (continue even if it fails)
        database_connected = await self.connect_database()
        if not database_connected:
            logger.warning("Bot starting in no-database mode - will skip database operations")
        
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
