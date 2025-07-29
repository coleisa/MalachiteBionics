// Push Notification Manager for Malachite Bionics Trading Alerts
// Handles subscription management and notification permissions

class PushNotificationManager {
    constructor() {
        this.swRegistration = null;
        this.isSubscribed = false;
        this.publicKey = null;
    }

    // Initialize the push notification system
    async init(publicKey) {
        this.publicKey = publicKey;
        
        if ('serviceWorker' in navigator && 'PushManager' in window) {
            try {
                // Register service worker
                this.swRegistration = await navigator.serviceWorker.register('/static/sw.js');
                console.log('Service Worker registered successfully');
                
                // Check if already subscribed
                const subscription = await this.swRegistration.pushManager.getSubscription();
                this.isSubscribed = !(subscription === null);
                
                if (this.isSubscribed) {
                    console.log('User is already subscribed to push notifications');
                }
                
                this.updateUI();
                return true;
            } catch (error) {
                console.error('Service Worker registration failed:', error);
                return false;
            }
        } else {
            console.warn('Push messaging is not supported');
            return false;
        }
    }

    // Request notification permission and subscribe
    async subscribe() {
        try {
            // Request notification permission
            const permission = await Notification.requestPermission();
            
            if (permission === 'granted') {
                // Subscribe to push notifications
                const subscription = await this.swRegistration.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: this.urlBase64ToUint8Array(this.publicKey)
                });

                console.log('User subscribed to push notifications');
                
                // Send subscription to server
                const response = await fetch('/api/push/subscribe', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(subscription)
                });

                if (response.ok) {
                    this.isSubscribed = true;
                    this.updateUI();
                    this.showStatus('Push notifications enabled!', 'success');
                    return true;
                } else {
                    throw new Error('Failed to save subscription on server');
                }
            } else {
                this.showStatus('Push notifications blocked. Please enable in browser settings.', 'error');
                return false;
            }
        } catch (error) {
            console.error('Failed to subscribe to push notifications:', error);
            this.showStatus('Failed to enable push notifications', 'error');
            return false;
        }
    }

    // Unsubscribe from push notifications
    async unsubscribe() {
        try {
            const subscription = await this.swRegistration.pushManager.getSubscription();
            
            if (subscription) {
                await subscription.unsubscribe();
                
                // Remove subscription from server
                const response = await fetch('/api/push/unsubscribe', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(subscription)
                });

                if (response.ok) {
                    this.isSubscribed = false;
                    this.updateUI();
                    this.showStatus('Push notifications disabled', 'info');
                    return true;
                }
            }
        } catch (error) {
            console.error('Failed to unsubscribe from push notifications:', error);
            this.showStatus('Failed to disable push notifications', 'error');
            return false;
        }
    }

    // Update UI based on subscription status
    updateUI() {
        const subscribeBtn = document.getElementById('subscribe-btn');
        const unsubscribeBtn = document.getElementById('unsubscribe-btn');
        const notificationStatus = document.getElementById('notification-status');

        if (subscribeBtn && unsubscribeBtn && notificationStatus) {
            if (this.isSubscribed) {
                subscribeBtn.style.display = 'none';
                unsubscribeBtn.style.display = 'inline-block';
                notificationStatus.textContent = 'Push notifications are enabled';
                notificationStatus.className = 'status-success';
            } else {
                subscribeBtn.style.display = 'inline-block';
                unsubscribeBtn.style.display = 'none';
                notificationStatus.textContent = 'Push notifications are disabled';
                notificationStatus.className = 'status-disabled';
            }
        }
    }

    // Show status message to user
    showStatus(message, type) {
        const statusDiv = document.createElement('div');
        statusDiv.className = `alert alert-${type === 'success' ? 'success' : type === 'error' ? 'danger' : 'info'}`;
        statusDiv.style.position = 'fixed';
        statusDiv.style.top = '20px';
        statusDiv.style.right = '20px';
        statusDiv.style.zIndex = '9999';
        statusDiv.style.padding = '10px 15px';
        statusDiv.style.borderRadius = '5px';
        statusDiv.style.maxWidth = '300px';
        statusDiv.textContent = message;

        document.body.appendChild(statusDiv);

        // Remove after 3 seconds
        setTimeout(() => {
            if (statusDiv.parentNode) {
                statusDiv.parentNode.removeChild(statusDiv);
            }
        }, 3000);
    }

    // Convert VAPID public key to Uint8Array
    urlBase64ToUint8Array(base64String) {
        const padding = '='.repeat((4 - base64String.length % 4) % 4);
        const base64 = (base64String + padding)
            .replace(/-/g, '+')
            .replace(/_/g, '/');

        const rawData = window.atob(base64);
        const outputArray = new Uint8Array(rawData.length);

        for (let i = 0; i < rawData.length; ++i) {
            outputArray[i] = rawData.charCodeAt(i);
        }
        return outputArray;
    }

    // Test notification (for admin testing)
    async testNotification() {
        if (this.isSubscribed) {
            try {
                const response = await fetch('/api/push/test', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    }
                });

                if (response.ok) {
                    this.showStatus('Test notification sent!', 'success');
                } else {
                    this.showStatus('Failed to send test notification', 'error');
                }
            } catch (error) {
                console.error('Failed to send test notification:', error);
                this.showStatus('Failed to send test notification', 'error');
            }
        } else {
            this.showStatus('Please enable push notifications first', 'error');
        }
    }
}

// Initialize when page loads
document.addEventListener('DOMContentLoaded', async () => {
    // Get VAPID public key from server
    try {
        const response = await fetch('/api/push/vapid-public-key');
        const data = await response.json();
        
        if (data.publicKey) {
            window.pushManager = new PushNotificationManager();
            await window.pushManager.init(data.publicKey);
        }
    } catch (error) {
        console.error('Failed to initialize push notifications:', error);
    }
});
