# TrafficToll
NetLimiter-like bandwidth limiting and QoS for Linux

# Description
TrafficToll allows you to limit download and upload bandwidth globally (per interface)
and per process, even during the process' runtime. Additionally it also allows you to
make use of QoS traffic prioritization for different processes.

The configuration can be easily adjusted and new limits and priorities applied at any
point, as opposed to similar tools which either can only apply fixed global limits to
the interface, certain ports, or require you to start the process through them (and thus
restart the target process to change the limits).

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
# The rate limits for the specified interface. Specifying these values is useful for two
# things: 1) you want to limit the used bandwidth for the entire interface or 2) you
# want to make use of traffic prioritization.

# If you want to 1) limit the used bandwidth for the entire interface, simply specify
# values below your actual speed: the traffic will be shaped in such a way, that it does
# not exceed the specified numbers.

# If you want to 2) make use of the traffic prioritization feature, these values must be
# as close as possible to your real speed: if they are too low, traffic prioritization
# will work, but you are losing part of your bandwidth; if they are too high, traffic
# prioritization won't work as well as it could. I recommend you use some internet speed
# test you can find online to get an approximation for the correct values.

# If you don't want to do 1) or 2), you can omit these values. Bandwidth limiting per
# application will still work, just traffic prioritization won't work as well or
# entirely.
download: 5mbps
upload: 1mbps

# Guaranteed download and upload rates for all global traffic that is not shaped as part
# of a matched process by TrafficToll. The idea here is to leave enough "guaranteed"
# bandwidth to all applications not defined in "processes", so that they are not starved
# to a bandwidth, by processes with higher priority, that would cause the other IP to
# drop the connection. These are the default values, if omitted. Keep in mind that this
# doesn't reserve the bandwidth -- if this traffic is not made use of, it's available
# to processes with higher priority.
download-minimum: 100kbps
upload-minimum: 10kbps

# A list of processes you want to match and their respective settings
processes:
  # You can name the process what you want, it is only used to identify it on the CLI
  # output
  "Path of Exile":
    # Adjust the traffic priority to 0 (highest possible, higher integers mean _lower_
    # priority) to prevent lag and high pings in the game even when we create heavy
    # traffic otherwise. If these priorities are omitted, they will default to 0: the
    # same priority for all the traffic on the interface. As soon as you explicitly
    # specify a priority for a process, the other traffic on the interface will get a
    # lower priority, so will other processes where you did not explicitly specify
    # another priority. In this case "Path of Exile" traffic will have a priority of 0,
    # the highest, and the interface traffic and other processes will have a priority of
    # 1.
    download-priority: 0
    upload-priority: 0

    # Download and upload rate limits can be entirely omitted if you don't want to apply
    # any, in this case traffic will only be prioritized like specified.
    #download:
    #upload:

    # The match section. A process is selected when all predicates in the match section
    # match. Every attribute psutil.Process
    # (https://psutil.readthedocs.io/en/latest/index.html#psutil.Process) provides on
    # Linux can be matched on, using regular expressions. Integer attributes will be
    # treated as strings and list attributes will be joined using a space before
    # matching. If you want to, you can also specify a regular expression with an OR
    # operator and match many processes which will all share the specified bandwidth
    # limit or traffic priority.
    # If you do not see a line starting with "Shaping traffic for..." with your process
    # name in it, while it is clearly causing traffic, your match section is failing.
    # Please make sure it works correctly.
    match:
      - name: "PathOfExile_x64"

  Vivaldi:
    # Additionally specify fixed bandwidth limits for the browser. Setting bandwidth
    # limits higher than the interface limits will not work. Different processes
    # borrow available traffic from the interface limits using their specified priority.
    download: 2500kbps
    upload: 500kbps

    # Explicitly set a lower download and upload priority to Path of Exile so our
    # browsing does not cause the game's ping to spike. This would have happened
    # automatically if we omitted it, because we specified a priority for "Path of
    # Exile" -- we are just doing it here for clarity.
    download-priority: 1
    upload-priority: 1
    match:
      - exe: /opt/vivaldi/vivaldi-bin

  Discord:
    # Set Discord's traffic priority to the lowest: this means if we create traffic via
    # either "Path of Exile" or "Vivaldi" it will get priority and Discord's latency and
    # traffic will slow down accordingly
    download-priority: 2
    upload-priority: 2

    # Additionally specify fixed bandwidth limits for Discord. Please note that just
    # because we specified 50% of the interface bandwidth for "Vivaldi" and "Discord"
    # each, does not mean "Path of Exile" or other processes will be starved for
    # traffic: Because we omitted download and upload limits for "Path of Exile" 100% of
    # the interface rate is automatically assumed for that process, in this case
    # 5mbps/1mbps. Additionally, because "Path of Exile" has a higher priority than
    # either of the two, in the extreme case that Vivaldi and Discord utilize their
    # bandwidth limits fully (and thus the entire interface's speed), "Path of Exile"
    # traffic will get priority and traffic for Vivaldi and Discord will be
    # appropriately reduced.
    download: 2500kbps
    upload: 500kbps

    match:
      - exe: /opt/discord/Discord

  Riot:
    download-priority: 2
    upload-priority: 2

    # The process that actually creates network traffic for electron-based applications
    # is not uniquely identifiable. Instead we match a uniquely identifiable parent
    # process, in this case "riot-desktop", and set recursive to True. This instructs
    # TrafficToll to traffic shape the connections of the matched process and all its
    # descendants
    recursive: True
    match:
      - name: riot-desktop

  JDownloader 2:
    download: 300kbps
    # The download-priority and upload-priority if omitted while another process
    # explicitly specifies them will automatically be the lowest: in this case 2, the
    # same as "Discord", our lowest priority process.

    # Since the download and upload priority of this process is the lowest, make sure
    # that its connections don't starve when processes with higher priority use up all
    # the available bandwidth. These are the default values for each process and will be
    # applied if omitted.
    download-minimum: 10kbps
    upload-minimum: 1kbps

    # JDownloader 2 obviously has its own bandwidth limiting, this is just here as an
    # example to show that matching on something else than the executable's name and
    # path is possible
    match:
      - cmdline: .* JDownloader.jar
```

Units can be specified in all formats that `tc` supports, namely: bit (with and without
suffix), kbit, mbit, gbit, tbit, bps, kbps, mbps, gbps, tbps. To specify in IEC units,
replace the SI prefix (k-, m-, g-, t-) with IEC prefix (ki-, mi-, gi- and ti-)
respectively.

All limits and priorities can be omitted, in which case obviously no traffic shaping
will be applied. A process is selected when all predicates in the match section match.
Every attribute
[`psutil.Process`](https://psutil.readthedocs.io/en/latest/index.html#psutil.Process)
provides on Linux can be matched on, using regular expressions.

When you terminate `tt` using Ctrl+C all changes to the traffic scheduling will be
reverted, allowing you to easily update the config and apply new limits.

# Installation
`$ pip install traffictoll`

`tt` has to be run as root.

# Screenshots
Because a picture is always nice, even for CLI applications:

![](https://i.imgur.com/a3U5Zdt.png)
