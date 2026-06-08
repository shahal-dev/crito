# INDI simulator server for the Phase-0 virtual site.
# Provides the telescope + CCD simulator drivers on TCP 7624.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends software-properties-common ca-certificates \
 && add-apt-repository universe \
 && apt-get update \
 && apt-get install -y --no-install-recommends indi-bin \
 && rm -rf /var/lib/apt/lists/*

EXPOSE 7624

# -v: verbose; add more simulator drivers here as later phases need them
CMD ["indiserver", "-v", \
     "indi_simulator_telescope", "indi_simulator_ccd", \
     "indi_simulator_focus", "indi_simulator_wheel"]
