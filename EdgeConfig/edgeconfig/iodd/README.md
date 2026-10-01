# IODD folder

Put the **IODD** of an IO-Link device (XML or zip) here. `Browse...` in the
commissioning tool opens on this folder, and a file whose VendorID/DeviceID
matches is picked up automatically.

**Process data only becomes values with an IODD.** Which bit means what, and
what the raw value has to be scaled by to become a physical quantity, differs
per device and lives only in the IODD. Without one you see bytes.

## What ships with it

The IODDs of the two SMC devices used during testing.

| File | Device |
|---|---|
| `SMC-PF3A808H-L2x-xxx-*-IODD1.1.xml` | PF3A808H flow meter |
| `SMC-AMS-ITV-*-IODD1.1.xml` | AMS-ITV electro-pneumatic regulator |

## Using another device

Get the IODD the manufacturer publishes and drop it in this folder - from their
website or from [IODDfinder](https://ioddfinder.io-link.com). A zip can be left
as it is.

Each IODD is subject to its manufacturer's distribution terms.
