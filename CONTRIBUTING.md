# Contributing

Thanks for helping improve JTech Grid Control, please help me make this even better! 

## Testing without a charger

`simulator.py` pretends to be a TeltoCharge, so you can exercise the whole flow
on your laptop:

```bash
# one terminal
python central_system.py

# another
python simulator.py --id EVC121 --plugged
```

`--plugged` makes it report a cable in the car, which triggers auto-start.

## The dashboard has no build step

`static/index.html` is plain HTML, CSS and JS in one file. Edit it directly.
Because of the strict CSP, wire controls through the `ACTIONS` map with
`data-act` attributes rather than inline `onclick`, and use CSS classes rather
than inline `style`. `check.py` enforces this.

## Reporting hardware compatibility

If you run this against a charger other than the EVC121, please open an issue
saying what worked and what didn't — especially charging modes and the
measurands your charger reports (the dashboard has a "Measurands seen" button).
