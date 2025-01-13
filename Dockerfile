FROM debian:bookworm

# Install dependencies
RUN apt-get update \
    && apt-get install -y python3-venv python3-pip git libogg-dev libopusenc-dev libflac-dev libopusfile-dev libopus-dev libvorbis-dev libopus0



# Create virtual environment for bridge
RUN mkdir -p /opt/asl-zello-bridge/venv \
    && python3 -m venv /opt/asl-zello-bridge/venv

# Install PyOgg to venv
RUN cd /opt \
    && git clone https://github.com/TeamPyOgg/PyOgg.git \
    && cd PyOgg && /opt/asl-zello-bridge/venv/bin/python setup.py install

# Install bridge
ADD . /opt/asl-zello-bridge
RUN cd /opt/asl-zello-bridge \
    && /opt/asl-zello-bridge/venv/bin/pip3 install .

# Cleanup
RUN apt-get clean

CMD ["/opt/asl-zello-bridge/venv/bin/asl-zello-bridge"]
