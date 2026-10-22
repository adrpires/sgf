#!/bin/sh
set -eu

mkdir -p /etc/nginx/certs

cert=""
key=""
if [ -s /certs-in/fullchain.pem ] && [ -s /certs-in/privkey.pem ]; then
  cert=/certs-in/fullchain.pem
  key=/certs-in/privkey.pem
elif [ -s /certs-in/ssl.cert ] && [ -s /certs-in/ssl.key ]; then
  cert=/certs-in/ssl.cert
  key=/certs-in/ssl.key
fi

if [ -n "$cert" ]; then
  cp "$cert" /etc/nginx/certs/fullchain.pem
  cp "$key" /etc/nginx/certs/privkey.pem
else
  nome="${GEFIN_SSL_CN:-apsitelecom.com.br}"
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout /etc/nginx/certs/privkey.pem \
    -out /etc/nginx/certs/fullchain.pem \
    -subj "/CN=${nome}" \
    -addext "subjectAltName=DNS:${nome}"
  echo "Sem certificado do Virtualmin em ./certs. Foi gerado um certificado temporário para ${nome}."
fi

exec nginx -g "daemon off;"
