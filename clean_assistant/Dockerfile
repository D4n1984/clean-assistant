# App (add-on) de Clean Assistant para Home Assistant.
FROM alpine:3.20

ENV PYTHONUNBUFFERED=1
# tzdata es imprescindible: sin la base de zonas horarias, poner TZ + time.tzset() no surte
# efecto en Alpine y los horarios se disparan con desfase. Ver backend/app.py _apply_host_timezone.
RUN apk add --no-cache python3 py3-pip openssl openssh-client-default jq git curl tzdata py3-cryptography

# Home Assistant builds this tag locally. CI can supply its own source ref.
ARG APP_REF=conga-0.37.6.2
RUN git clone --depth 1 --branch "${APP_REF}" https://github.com/D4n1984/clean-assistant /app \
    && pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
