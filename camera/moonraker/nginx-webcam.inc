# nginx location for the Eitri camera. Include inside the server {} block that
# serves Fluidd/Mainsail on port 80 (for example: include /etc/nginx/eitri/nginx-webcam.inc;).
#
# ustreamer serves /stream and /snapshot at its root, so the trailing slash on
# proxy_pass strips the /webcam/ prefix. Buffering is off so frames reach the
# browser as they arrive, instead of batching up in nginx.

location /webcam/ {
    proxy_pass http://127.0.0.1:8080/;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 1h;
    proxy_set_header Connection "";
    add_header Cache-Control "no-store";
}
