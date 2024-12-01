# Allstarlink <=> Zello Bridge
This tool allows Zello Free or Zello Work channels to be connected to USRP, enabling bridging of amateur radio networks to the Zello network.

Tested with:
- [Allstarlink](https://www.allstarlink.org/) `chan_usrp`
- [DVSwitch](https://dvswitch.groups.io/g/main?) `Analog_Bridge`
- [MMDVM_CM](https://github.com/juribeparada/MMDVM_CM) `USRP2DMR` and `USRP2YSF`
- [SvxLink](https://www.svxlink.org/) via a third-party USRP module

The original inspiration for this project was the work done by [Rob G4ZWH](https://www.qrz.com/db/G4ZWH) to build a public Zello bridge to the [FreeSTAR](https://freestar.network/) network using SIP softphones and the Zello Windows client. This was extremely well received by users of the network, however it was not an ideal solution and had some shortcomings. [Matt G4IYT](https://www.qrz.com/db/G4IYT) later rebuilt the bridge as a dedicated service based on the [Zello Channels API](https://github.com/zelloptt/zello-channel-api/blob/master/API.md).

Current users of the bridge include
- [FreeSTAR](https://freestar.network)
- [CumbriaCQ.com](https://cumbriacq.com/)
- [235 Alive](https://235alive.com)

The bridge does not require a lot of resources. The FreeSTAR bridge runs on a small VPS with 1 VCPU and 1GB RAM, and runs well on AMD64 and ARM platforms.

## Installation
These instructions were tested with Debian 12, and may need adaptation for other systems.

### Install `pyogg`
Current version of pyogg available through pip is not up to date, so install from git.
```
sudo apt-get install libopus0 libopus-dev
git clone https://github.com/TeamPyOgg/PyOgg.git
cd PyOgg
sudo python setup.py install
```
### Install Bridge
```
git clone https://github.com/mattmelling/asl-zello-bridge.git
cd asl-zello-bridge
sudo apt-get install -y python3-setuptools
sudo python3 setup.py install
```

Now `asl_zello_bridge` should be on your `$PATH`.

### Setup Service
```
sudo cp asl-zello-bridge.service /etc/systemd/system/
sudo systemctl edit asl-zello-bridge.service
```

Update environment variables by setting this in the editor that pops up:

```
[Service]
# Bind host for USRP RX
Environment=USRP_BIND=
# Destination host for USRP TX
Environment=USRP_HOST=
# Port we receive USRP stream on
Environment=USRP_RXPORT=
# Port we transmit USRP stream on
Environment=USRP_TXPORT=

Environment=ZELLO_USERNAME=
Environment=ZELLO_PASSWORD=
Environment=ZELLO_CHANNEL=

# Change this for different Zello flavor, see below
Environment=ZELLO_WS_ENDPOINT=wss://zello.io/ws
```

#### Zello Free
For Zello Free accounts, also set the following:

- `ZELLO_PRIVATE_KEY` should be a path to your PKCS#8 format private key, from the Zello Developers Console.
- `ZELLO_ISSUER` should be set to the issuer, also from the Zello Developers Console.

#### Zello Work
For Zello Work accounts, set the following additional configuration:

- `ZELLO_API_ENDPOINT` should be set to your Zello network, e.g. `https://mynetwork.zellowork.com`
- `ZELLO_WS_ENDPOINT` should be your network's websocket endpoint, e.g. `ws://zellowork.io/ws/mynetwork`

#### Additional Environment Variables
These extra environment variables are entirely optional, shown with their defaults

```
# Specify format for log messages, see https://docs.python.org/3/library/logging.html#logrecord-attributes
LOG_FORMAT="%(levelname)s:%(name)s:%(message)s"

# Gain applied at USRP interface in dB, defaults to 0dB
Environment=USRP_GAIN_RX_DB=0

# TX = USRP stream output to ASL, RX = USRP stream from ASL
Environment=USRP_GAIN_TX_DB=0
```

#### Enable Service
Finally, enable the service

```
sudo systemctl enable asl-zello-bridge.service
sudo systemctl start asl-zello-bridge.service
```

## Allstarlink Setup
Set up a USRP channel in ASL:

rpt.conf:

```
[1001]
rxchannel = USRP/127.0.0.1:7070:7071
```

## Credits
`asl-zello-bridge` is built and maintained by [Matt G4IYT](https://www.qrz.com/db/G4IYT)

Special thanks to:
- [Rob G4ZWH](https://www.qrz.com/db/G4ZWH) for inspiration with the original "janky" bridge, hosting the FreeSTAR bridge, and continued support 
- [Shane M0VUB](https://www.qrz.com/db/M0VUB) for his continued support on behalf of the FreeSTAR network
- [Lee M0LLC](https://www.qrz.com/db/M0LLC) for early adoption and testing with the CumbriaCQ network
- [Piotr G0TWP](https://www.qrz.com/db/G0TWP) for testing with SvxLink and unearthing some tricky bugs
