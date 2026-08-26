# JCo REST bridge

A small pure-JDK HTTP service that wraps SAP Java Connector (JCo) and exposes three
endpoints on `127.0.0.1:8080`:

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | liveness |
| `POST /api/table/read` | `RFC_READ_TABLE`, returns rows |
| `POST /api/rfc/call` | one function module |
| `POST /api/rfc/batch` | a sequence in one stateful `JCoContext` (needed for the CCMS host-metrics calls) |

## Read-only by construction

The bridge holds an **allow-list** of nine function modules and refuses anything
else with **HTTP 403** before it opens a connection to SAP:

```
RFC_READ_TABLE  RFC_SYSTEM_INFO  TH_WPINFO  ENQUEUE_READ
BAPI_XMI_LOGON  BAPI_XMI_LOGOFF
BAPI_SYSTEM_MS_GETLIST  BAPI_SYSTEM_MON_GETTREE  BAPI_SYSTEM_MTE_GETPERFCURVAL
```

All nine read; none changes anything in SAP. The list can be overridden for
testing with the `JCO_ALLOWED_FUNCTIONS` environment variable (comma-separated),
but the default is the nine above.

## Build

You must supply the SAP JCo library yourself — it is proprietary and cannot be
redistributed here.

1. Download **SAP Java Connector 3.1** from the SAP Support Portal
   (https://support.sap.com, search "SAP Java Connector").
2. Place `sapjco3.jar` and the native library (`libsapjco3.so` on Linux,
   `sapjco3.dll` on Windows) in this `bridge/` directory.
3. Build and run:

   ```bash
   ./build.sh
   java -cp .:sapjco3.jar -Djava.library.path=. SapJcoRest
   ```

Connection parameters (host, system number, client, user, password) are supplied
per request by the MCP server, so the bridge stores no SAP credentials itself.
