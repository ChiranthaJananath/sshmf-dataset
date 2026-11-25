# Key-exchange microbenchmark image: OpenSSH 10 (supports sntrup761, ML-KEM, X25519).
FROM alpine:3.22
RUN apk add --no-cache openssh-server openssh-client openssh-keygen python3 \
 && sed -i 's/^root:!/root:*/' /etc/shadow \
 && mkdir -p /root/.ssh && chmod 700 /root/.ssh && ssh-keygen -A
COPY kexbench.py entrypoint.sh /
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
