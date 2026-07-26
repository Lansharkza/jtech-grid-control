# Putting it on your iPhone

## Add to Home Screen (recommended)

1. Open `http://192.168.0.121:8081/` in **Safari** (not Chrome — only Safari can
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

## If you want a real native app

Realistic requirements:

- A **Mac** with Xcode. There is no supported way to build an iOS app without one.
- An **Apple Developer account**, $99/year. Without it a sideloaded build stops
  working after 7 days and must be reinstalled.
- A second codebase to maintain, in Swift, showing the same numbers.

Two routes:

**Capacitor** wraps this web app in a native shell. You keep one codebase, gain
push notification capability and App Store distribution. Still needs the Mac and
the developer account. Roughly: `npm init @capacitor/app`, point the config's
`server.url` at the dashboard, `npx cap add ios`, open in Xcode, build.

**SwiftUI from scratch** talks to the existing REST API — `/api/chargers`,
`/api/settings`, `/api/history` and the control endpoints are all JSON and would
back a native UI directly. This buys you native charts, widgets and Live
Activities, at the cost of rewriting the interface and keeping two things in
step.

For a single-user home charger dashboard, the home screen route gives you most
of the benefit for none of the cost. I would only go native if you specifically
want push notifications when charging finishes, or a Lock Screen widget.

One thing worth knowing if you do go native: iOS App Transport Security blocks
plain HTTP by default, so a native app talking to `http://192.168.0.121:8081`
needs an ATS exception in Info.plist — or, better, HTTPS on the server.

## Trusting the certificate on iOS

A self-signed certificate is real encryption, but iOS does not know who signed
it, so Safari shows a warning and refuses to install the app to the home screen
until you trust it. Two steps, and both are required:

1. **Install it.** Email `certs/server.crt` to yourself, or put it in Files, and
   open it on the iPhone. iOS says a profile was downloaded. Go to
   **Settings → General → VPN & Device Management**, tap the profile, **Install**.
2. **Trust it.** This is the step people miss and nothing works without it:
   **Settings → General → About → Certificate Trust Settings**, then enable the
   toggle next to the certificate.

Then reload the dashboard. The padlock appears and Add to Home Screen behaves.

Repeat on each device. The certificate is valid for 825 days — the maximum iOS
accepts — after which regenerate and reinstall.

## Or skip certificates entirely

**Tailscale** gives every device a stable name and a genuinely trusted HTTPS
certificate, with no port forwarding, no self-signed warnings, and it works from
anywhere rather than only on your Wi-Fi. Install it on the NAS from Package
Center and on the iPhone from the App Store. That also unlocks the two things
plain HTTP blocks: web push notifications and offline caching.

For a single-user charger dashboard this is less work than distributing a
certificate to every device, and it solves remote access at the same time.
