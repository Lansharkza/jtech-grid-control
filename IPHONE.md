# Putting it on your iPhone

## Add to Home Screen (recommended)

1. Open `http://remote.yourdomain.com:8081/` in **Safari** (not Chrome — only Safari can
   install to the home screen on iOS).
2. Tap the **Share** button, then **Add to Home Screen**.
3. It appears as "Grid Control" with the hexagon icon.

Launched from that icon it runs full screen with no Safari address bar, its own
app switcher entry, and the dark status bar. Tick **Stay signed in for 30 days**
on the login page so it does not ask every time you open it.

## What this is and is not

It is the same web app, so there is one codebase and updates arrive the moment
you rebuild the container — nothing to reinstall.

It is not an App Store app. Specifically:

- **It only works where the server is reachable.** On your home Wi-Fi that is
  fine. Away from home you need a VPN back to the NAS (Tailscale is the least
  effort) or the dashboard exposed over HTTPS.
- **No push notifications.** iOS supports web push only for HTTPS sites, and
  only since iOS 16.4. Over plain HTTP on the LAN there is no route to
  "your car finished charging" alerts.
- **No offline caching.** Service workers require a secure context, so over
  `http://` the page needs the server time. That is fine here — a charger
  dashboard with stale data would be misleading anyway.

Both limitations disappear if you put HTTPS in front of it.

