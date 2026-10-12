FROM alpine:3.24.2

RUN apk add --no-cache                      \
    openssh                                 \
    openmpi openmpi-dev                     \
    python3 py3-pip py3-mpi4py py3-numpy 

RUN ssh-keygen -f ~/.ssh/id_ed25519 -N '' &&\
    sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config &&\
    echo "root:root" | chpasswd

COPY --chmod=755 ./entrypoint.sh ./entrypoint.sh

ENV OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1

WORKDIR /workdir

EXPOSE 22
ENTRYPOINT ["./entrypoint.sh"]
CMD ["ash"]
