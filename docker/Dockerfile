FROM python:alpine3.18

COPY entrypoint /entrypoint

ENTRYPOINT [ "/entrypoint" ]

RUN adduser -D -u 54000 zello && \
        apk update && \
        apk add git gcc musl-dev opus libffi-dev cargo openssl-dev && \
        pip install --upgrade pip setuptools wheel && \
        pip cache purge && \
        pip install cryptography aiohttp pyjwt pyogg && \
        git clone https://github.com/mattmelling/asl-zello-bridge.git asl-zello-bridge && \
        cd /asl-zello-bridge && \
        python setup.py install && \
        chown -R zello: /asl-zello-bridge
USER zello

ENTRYPOINT [ "/entrypoint" ]