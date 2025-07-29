// Service Worker for Push Notifications
// This handles receiving and displaying push notifications

self.addEventListener('install', (event) => {
    console.log('Service Worker installed');
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    console.log('Service Worker activated');
    event.waitUntil(self.clients.claim());
});

// Handle push notifications
self.addEventListener('push', (event) => {
    console.log('Push notification received:', event);
    
    if (event.data) {
        try {
            const data = event.data.json();
            console.log('Push notification data:', data);
            
            const options = {
                body: data.body,
                icon: data.icon || '/static/icon-192x192.png',
                badge: data.badge || '/static/badge-72x72.png',
                data: data.data,
                actions: data.actions || [],
                requireInteraction: data.requireInteraction || false,
                tag: data.tag || 'trading-alert',
                vibrate: [200, 100, 200], // Vibration pattern for mobile
                timestamp: Date.now()
            };
            
            event.waitUntil(
                self.registration.showNotification(data.title, options)
            );
        } catch (error) {
            console.error('Error parsing push notification data:', error);
            // Fallback notification
            event.waitUntil(
                self.registration.showNotification('Trading Alert', {
                    body: 'You have a new trading alert',
                    icon: '/static/icon-192x192.png'
                })
            );
        }
    }
});

// Handle notification clicks
self.addEventListener('notificationclick', (event) => {
    console.log('Notification clicked:', event);
    
    event.notification.close();
    
    if (event.action === 'view') {
        // Open the alerts page
        event.waitUntil(
            clients.openWindow(event.notification.data.url || '/alerts')
        );
    } else if (event.action === 'dismiss') {
        // Just close the notification
        return;
    } else {
        // Default action - open the alerts page
        event.waitUntil(
            clients.openWindow('/alerts')
        );
    }
});

// Handle notification close
self.addEventListener('notificationclose', (event) => {
    console.log('Notification closed:', event);
});
