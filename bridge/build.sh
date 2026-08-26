#!/usr/bin/env bash
# Build the JCo REST bridge. Requires Java 17+ and the SAP JCo 3.1 library.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f sapjco3.jar ]]; then
  echo "sapjco3.jar not found. Download SAP JCo 3.1 from the SAP Support Portal"
  echo "(https://support.sap.com, 'SAP Java Connector') and place sapjco3.jar and"
  echo "the native library (libsapjco3.so on Linux, sapjco3.dll on Windows) here."
  exit 1
fi

javac -cp .:sapjco3.jar SapJcoRest.java
echo "Built. Run with:"
echo "  java -cp .:sapjco3.jar -Djava.library.path=. SapJcoRest"
echo "The bridge listens on 127.0.0.1:8080 and refuses any function module"
echo "outside its allow-list with HTTP 403."
