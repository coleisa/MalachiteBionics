# Trading Bot Service Website

A professional Flask web application for selling Discord trading bot services with Stripe payment integration.

## Features

- User authentication and registration
- Stripe subscription management
- Multiple trading algorithm versions (v3, v6, v9)
- PostgreSQL database integration
- Railway deployment ready
- Minimalistic black and white design
- Admin dashboard for customer management

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set environment variables:
```bash
cp .env.example .env
# Edit .env with your actual values
```

3. Initialize database:
```bash
python init_db.py
```

4. Run the application:
```bash
python app.py
```

## Deployment

This application is configured for Railway deployment. Just connect your GitHub repository to Railway and it will automatically deploy.

## Environment Variables

- `SECRET_KEY`: Flask secret key
- `DATABASE_URL`: PostgreSQL database URL
- `STRIPE_PUBLISHABLE_KEY`: Stripe publishable key
- `STRIPE_SECRET_KEY`: Stripe secret key
- `STRIPE_WEBHOOK_SECRET`: Stripe webhook secret
