# TrafficToll
NetLimiter-like traffic shaping for Linux

# Description
TrafficToll allows you to limit download and upload bandwidth globally
(per interface) and per process, even during the process' runtime.

The configuration can be easily adjusted and new limits applied at any
point, as opposed to similar tools which either can only apply fixed
global limits to the interface, certain ports, or require you to start
the process through them (and thus restart the target process to change
the limits).

# Usage
`# tt device config`

Where `device` is the interface you want to limit (usually the one you
connect to the internet with). For example:

* `# tt enp3s0 night.yaml --delay 0.5` (regular interface, check every
half second for change in networked processes)
* `# tt tun0 day.yaml --logging-level DEBUG` (VPN interface, adjust
logging level to DEBUG)

Currently TrafficToll works based on a YAML configuration file. The configuration file
is best explained by example:

```YAML
# Global limits
download: 500kbps
upload: 100kbps

# Matched process limits
processes:
  Vivaldi:
    download: 100kbps
    match:
      - exe: /opt/vivaldi/vivaldi-bin

  Discord:
    download: 300kbps

    # This won't work, the specified upload exceeds the global upload, it will
    # be 100kb/s max
    upload: 200kbps
    match:
      - exe: /opt/discord/Discord

  JDownloader 2:
    # JDownloader 2 obviously has its own traffic shaping, this is just here as
    # an example to show that matching on something else than the executable's
    # path is possible
    download: 300kbps
    match:
      - cmdline: .* JDownloader.jar
```

Units can be specified in all formats that `tc` supports, namely: bit 
(with and without suffix), kbit, mbit, gbit, tbit, bps, kbps, mbps,
gbps, tbps. To specify in IEC units, replace the SI prefix (k-, m-, g-,
t-) with IEC prefix (ki-, mi-, gi- and ti-) respectively.

All limits can be omitted, in which case obviously no limiting will be
applied. A process is selected when all predicates in the match section
match. Every attribute [`psutil.Process`](https://psutil.readthedocs.io/en/latest/index.html#psutil.Process)
provides on Linux can be matched on, using regular expressions.

When you terminate `tt` using Ctrl+C all changes to the traffic
scheduling will be reverted, allowing you to easily update the config
and apply new limits.

# Installation
`$ pip install traffictoll`

`tt` has to be run as root.

# Screenshots
Because a picture is always nice, even for CLI applications:

![](https://i.imgur.com/EsOla66.png)
