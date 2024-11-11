# Allstarlink <=> Zello Bridge

## Installation

### Install `pyogg`
Current version of pyogg available through pip is not up to date, so install from git.
```
sudo apt-get install libopus0
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
Environment=USRP_BIND=                         # Bind host for USRP RX
Environment=USRP_HOST=                         # Destination host for USRP TX
Environment=USRP_RXPORT=
Environment=USRP_TXPORT=
Environment=ZELLO_USERNAME=
Environment=ZELLO_PASSWORD=
Environment=ZELLO_CHANNEL=
Environment=ZELLO_ENDPOINT=wss://zello.io/ws   # Change this for different Zello flavor
```

For Zello Free accounts, also set the following:

- `ZELLO_PRIVATE_KEY` should be a path to your PKCS#8 format private key, from the Zello Developers Console.
- `ZELLO_ISSUER` should be set to the issuer, also from the Zello Developers Console.

Then enable the service

```
sudo systemctl enable asl-zello-bridge.service
```

## Allstarlink Setup
Set up a USRP channel in ASL:

rpt.conf:

```
[1001]
rxchannel = USRP/127.0.0.1:7070:7071
```
