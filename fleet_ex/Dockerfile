# Emulated managed host for E10: Ubuntu 24.04 with its own sshd (PAM enabled, as on the
# real Ubuntu targets), its own dockerd, and a non-root user in the docker group.
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y openssh-server docker.io curl \
 && mkdir -p /run/sshd \
 && useradd -m -s /bin/bash -G docker exp
VOLUME /var/lib/docker
COPY alpine.tar /alpine.tar
COPY start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
