#!/bin/sh
/usr/sbin/sshd
cat ~/.ssh/id*.pub
$@
