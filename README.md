# SAP Basis Copilot

Read-only Basis diagnostics for SAP NetWeaver and S/4HANA, from Claude Desktop.

Ask about system health, short dumps, background jobs, work processes, locks, RFC
queues, spool, and host resources in plain language; the server reads the answer
from the live system and reports it in the terms a Basis administrator uses. It
reads only — there is no function module in it that can change anything in SAP.

This is the Basis module of SyntaAI Agent. See https://syntaai.com.

## What it answers

25 tools, grouped by the transaction each one replaces:

- **Monitoring** — SM21 system log, SM13 update errors, SM51 instances, a combined health check
- **Performance** — SM50/SM66 work processes, ST03N response times, ST02 memory and buffers, DB02 database, RZ10/RZ11 parameters
- **Dumps** — ST22 short-dump summary, recurring faults, per-program trend, analysis
- **Jobs and tasks** — SM37 background jobs, STC01/STC02 task lists
- **Locks and queues** — SM12 lock entries, SM58 tRFC, SMQ1/SMQ2 qRFC
- **Spool** — SP01/SP02 requests and output
- **Host** — ST06/OS07N CPU, memory, paging, swap and filesystems, read from CCMS

## Setup (about five minutes)

You need Python 3.11+, Java 17+, and the SAP Java Connector (JCo) 3.1 library.

1. **Python environment**

   ```bash
   python -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

2. **Download SAP JCo 3.1** from the SAP Support Portal
   (https://support.sap.com, search "SAP Java Connector") and place `sapjco3.jar`
   and the native library (`libsapjco3.so` on Linux, `sapjco3.dll` on Windows)
   in `bridge/`. JCo is proprietary and is not included here.

3. **Build and start the bridge**

   ```bash
   cd bridge && ./build.sh
   java -cp .:sapjco3.jar -Djava.library.path=. SapJcoRest
   ```

   It listens on `127.0.0.1:8080` and refuses any function module outside its
   allow-list with HTTP 403.

4. **Configure the SAP connection** — copy `.env.example` to `.env` and fill in
   `SAP_ASHOST`, `SAP_SYSNR`, `SAP_CLIENT`, `SAP_USER`, `SAP_PASSWD`. Use a
   dedicated read-only RFC user (see the authorizations below).

5. **Add to Claude Desktop** — in `claude_desktop_config.json`:

   ```json
   {
     "mcpServers": {
       "sap-basis-copilot": {
         "command": "/absolute/path/to/sap-basis-copilot/venv/bin/python",
         "args": ["/absolute/path/to/sap-basis-copilot/server.py"],
         "env": { "MCP_TRANSPORT": "stdio" }
       }
     }
   }
   ```

   Restart Claude Desktop. The 25 tools appear under this server.

## Tool catalogue

| Tool | Tcode | What it answers |
|------|-------|-----------------|
| `get_system_health_summary` | SM51/SM37/SM21 | One-shot health check: instances up, active jobs, critical syslog |
| `get_instance_status` | SM51 | Application server instances and their status |
| `get_syslog_critical` | SM21 | Recent critical / error system-log entries (see note) |
| `check_update_errors` | SM13 | Failed update requests, top failing modules |
| `get_work_process_status` | SM50/SM66 | Work processes across instances — active, waiting, stopped |
| `get_response_time_analysis` | ST03N | Dialog / batch / RFC response-time statistics |
| `get_database_performance` | ST05/DB02 | Database object statistics and general DB info |
| `get_memory_usage` | ST02 | Memory allocation vs configured limits per instance |
| `get_parameter_recommendations` | RZ10/RZ11 | Profile parameters compared against best practice |
| `analyze_buffer_performance` | ST02 | Buffer hit ratios and cache efficiency |
| `get_background_jobs` | SM37 | Background jobs by status (all / active / finished / cancelled) |
| `get_client_settings` | SCC4 | Client settings (T000): cross-client access, change recording |
| `check_snc_configuration` | RZ10 | Secure Network Communication status |
| `get_dump_summary` | ST22 | ABAP short-dump summary: totals, top errors, programs, users |
| `get_repeat_dumps` | ST22 | Recurring error + program combinations |
| `get_dumps_by_program` | ST22 | All dumps for one program, with trend |
| `get_dump_analysis` | ST22 | Pattern detection, root-cause hypothesis, action plan |
| `get_task_lists` | STC01 | Available technical-configuration task lists |
| `get_task_list_details` | STC01 | Steps within one task list |
| `get_task_list_runs` | STC02 | Task-list execution history |
| `get_lock_entries` | SM12 | Current lock entries: who holds what, for how long, which are stale |
| `get_trfc_queue_status` | SM58 | Transactional-RFC status: stuck and failed calls |
| `get_qrfc_queue_status` | SMQ1/SMQ2 | Queued-RFC status: queue depth, held and failed queues |
| `get_spool_status` | SP01/SP02 | Spool and output requests: failed prints, retention backlog |
| `get_os_resource_snapshot` | ST06/OS07N | Host CPU, memory, paging, swap, filesystems (via CCMS) |

> **One current limitation, stated plainly.** `get_syslog_critical` (SM21) returns an
> "unavailable" result rather than syslog entries on systems without CCMS central
> monitoring configured. The classic reader `RSLG_READ_SYSLOG_FOR_PERIOD` is not a
> remote-enabled function module, and the CCMS alternative
> (`SALC_MSC_READ_SYSLOG`) needs a configured monitoring destination that a plain
> RFC user does not have. The tool degrades cleanly to a note; the system log is on
> the roadmap. This is noted again under "What it does not do".

## What it reads

Every SAP object each tool touches, generated from the code:

| Tool | Tcode | Tables read | Function modules |
|------|-------|-------------|------------------|
| `get_system_health_summary` | SM51/SM37/SM21 | TBTCO | RFC_SYSTEM_INFO |
| `get_instance_status` | SM51 | — | RFC_SYSTEM_INFO |
| `get_syslog_critical` | SM21 | — | RSLG_READ_SYSLOG_FOR_PERIOD |
| `check_update_errors` | SM13 | VBHDR | — |
| `get_work_process_status` | SM50/SM66 | — | TH_WPINFO |
| `get_response_time_analysis` | ST03N | — | TH_WPINFO |
| `get_database_performance` | ST05/DB02 | DBSTATC | — |
| `get_memory_usage` | ST02 | — | TH_WPINFO |
| `get_parameter_recommendations` | RZ10/RZ11 | PAHI | — |
| `analyze_buffer_performance` | ST02 | — | TH_WPINFO |
| `get_background_jobs` | SM37 | TBTCO | — |
| `get_client_settings` | SCC4/T000 | T000 | — |
| `check_snc_configuration` | RZ10 | PAHI | — |
| `get_dump_summary` | ST22 | — | — |
| `get_repeat_dumps` | ST22 | — | — |
| `get_dumps_by_program` | ST22 | — | — |
| `get_dump_analysis` | ST22 | — | — |
| `get_task_lists` | STC01 | STC_BASSCN_HDR_T, STC_SCN_HDR, STC_SCN_HDR_T, STC_SCN_TASKS | — |
| `get_task_list_details` | STC01 | STC_SCN_ATTR, STC_SCN_HDR, STC_SCN_HDR_T, STC_SCN_TASKS, STC_TEMPLATE | — |
| `get_task_list_runs` | STC02 | STC_SCN_HDR_T, STC_SESSION | — |
| `get_lock_entries` | SM12 | — | ENQUEUE_READ |
| `get_trfc_queue_status` | SM58 | ARFCSSTATE | — |
| `get_qrfc_queue_status` | SMQ1/SMQ2 | TRFCQIN, TRFCQOUT | — |
| `get_spool_status` | SP01/SP02 | TSP01, TSP02 | — |
| `get_os_resource_snapshot` | ST06/OS07N | — | BAPI_SYSTEM_MON_GETTREE, BAPI_SYSTEM_MTE_GETPERFCURVAL |

**This server never reads the user master, authorization, or change-document
tables** (`USR*`, `AGR*`, `CDHDR`/`CDPOS`). It reads Basis operational tables and
calls read-only function modules only.

## The read-only guarantee

The bridge holds an allow-list of nine function modules and refuses anything else
with **HTTP 403 before it opens a connection to SAP**:

```
RFC_READ_TABLE  RFC_SYSTEM_INFO  TH_WPINFO  ENQUEUE_READ
BAPI_XMI_LOGON  BAPI_XMI_LOGOFF
BAPI_SYSTEM_MS_GETLIST  BAPI_SYSTEM_MON_GETTREE  BAPI_SYSTEM_MTE_GETPERFCURVAL
```

All nine read; `BAPI_XMI_LOGON`/`LOGOFF` only open and close the CCMS session the
host-metrics reads need. There is no create, change, delete, lock, or transaction
commit anywhere in the list. The list is enforced in the bridge, not merely in the
Python client, so it holds regardless of what the MCP server asks for.

## Minimum authorizations for the RFC user

Grant a dedicated RFC user only what the tools above need. **SAP_ALL is not
required and should not be used.**

| Object | Values | For |
|--------|--------|-----|
| `S_RFC` | `RFC_TYPE=FUGR`/`FUNC`; `RFC_NAME` = the nine permitted FMs plus their function groups (`RFC_READ_TABLE`→`SDTX`, `RFC_SYSTEM_INFO`→`SRFC`, `TH_WPINFO`→`SThreads`, `ENQUEUE_READ`→`SENA`, the `BAPI_XMI_*` and `BAPI_SYSTEM_*` calls→`SXMI`/`SALS`); `ACTVT=16` | calling the function modules |
| `S_TABU_NAM` (or `S_TABU_DIS` by table group) | `ACTVT=03`; `TABLE` = the tables in "What it reads" (`TBTCO, VBHDR, DBSTATC, PAHI, T000, ARFCSSTATE, TRFCQIN, TRFCQOUT, TSP01, TSP02, STC_*`) | reading tables via `RFC_READ_TABLE` |
| `S_ADMI_FCD` | `S_ADMI_FCD=ENQ` — verify on your system | reading lock entries (`ENQUEUE_READ`, SM12) |
| `S_RZL_ADM` | `ACTVT=03` | CCMS monitor access (ST06 host metrics) |
| `S_XMI_PROD` | `EXTCOMPANY=SyntaAI`, `EXTPRODUCT=MCP`, `INTERFACE=XAL` — verify on your system | the CCMS XMI session |

The exact function groups and the `S_ADMI_FCD` / `S_XMI_PROD` field values vary by
release and should be **verified on your system** with an authorization trace
(ST01 / STAUTHTRACE) on the RFC user during a first run. Start minimal and add only
what the trace shows missing.

## What it does not do — roadmap, stated plainly

- **No HANA system views.** It reads the ABAP/CCMS layer, not `M_*` HANA monitoring views. Native HANA diagnostics are a separate concern.
- **No operating-system commands.** Host metrics come from CCMS/saposcol over RFC. There is no SSH, no shell, and no SM49/SM69 external commands.
- **No execute, no change.** Every tool reads. Acting on what it finds — deleting a lock, cancelling a job, restarting a work process — is done by an administrator in SAP, not by this server.
- **System log (SM21) not yet delivered.** No remote-enabled syslog reader is available on a standard system: the classic `RSLG_READ_SYSLOG_FOR_PERIOD` is not remote-callable, and the CCMS reader needs central-monitoring configuration. `get_syslog_critical` returns a clear "unavailable" note until a supported source is wired in.

## License

Apache License 2.0. Copyright 2026 SyntaAI Technologies Private Limited.
