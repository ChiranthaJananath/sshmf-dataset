# Emulated managed host for E8: docker-in-docker + sshd (root, key only).
ARG DIND_TAG=29-dind
FROM docker:${DIND_TAG}
RUN apk add --no-cache openssh-server openssh-keygen \
 && sed -i 's/^root:!/root:*/' /etc/shadow \
 && mkdir -p /root/.ssh && chmod 700 /root/.ssh \
 && printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\nUseDNS no\n' >> /etc/ssh/sshd_config
COPY entrypoint.sh /sshmf-entrypoint.sh
RUN chmod +x /sshmf-entrypoint.sh
ENTRYPOINT ["/sshmf-entrypoint.sh"]
