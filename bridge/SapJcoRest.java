import com.sap.conn.jco.*;
import com.sap.conn.jco.ext.*;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;

/**
 * JCo REST bridge for SAP Basis Copilot.
 *
 * Pure-JDK HTTP server (no Spring / no Maven) that wraps SAP JCo and exposes:
 *   GET  /api/health
 *   POST /api/table/read   {connection,table,fields[],where,maxRows} -> {rows[],count}
 *   POST /api/rfc/call     {connection,function,parameters{}}        -> {export{},tables{}}
 *
 * Connection params (ashost,sysnr,client,user,passwd,lang) are supplied per
 * request by the MCP server, so this service stores no SAP credentials itself.
 *
 * Listens on 127.0.0.1:8080 (override with JCO_REST_PORT).
 * Native lib libsapjco3.so must be on LD_LIBRARY_PATH (same dir as this class).
 */
public class SapJcoRest {

    static final String HOST = System.getenv().getOrDefault("JCO_REST_HOST", "127.0.0.1");
    static final int    PORT = Integer.parseInt(System.getenv().getOrDefault("JCO_REST_PORT", "8080"));

    static final DynamicProvider PROVIDER = new DynamicProvider();

    /**
     * Function modules this bridge is permitted to execute.
     *
     * Until now /api/rfc/call would run any function module a local caller named.
     * This copy permits nine function modules and nothing else. All nine are
     * read-only: seven read, and BAPI_XMI_LOGON / BAPI_XMI_LOGOFF only open and
     * close the CCMS session that the host-metrics reads need. There is no
     * function module here that can change anything in SAP.
     *
     * Override with JCO_ALLOWED_FUNCTIONS (comma-separated). An empty entry is
     * ignored; names are upper-cased and trimmed.
     */
    static final String[] DEFAULT_ALLOWED = {
        // --- generic / infrastructure ---
        "RFC_READ_TABLE", "RFC_SYSTEM_INFO", "TH_WPINFO",
        // --- lock entries (SM12) ---
        "ENQUEUE_READ",
        // --- CCMS host metrics (stateful XAL sequence, via /api/rfc/batch) ---
        "BAPI_XMI_LOGON", "BAPI_XMI_LOGOFF",
        "BAPI_SYSTEM_MS_GETLIST", "BAPI_SYSTEM_MON_GETTREE", "BAPI_SYSTEM_MTE_GETPERFCURVAL",
    };

    static final Set<String> ALLOWED_FUNCTIONS = buildAllowList();


    static Set<String> buildAllowList() {
        String env = System.getenv("JCO_ALLOWED_FUNCTIONS");
        Set<String> out = new LinkedHashSet<>();
        if (env != null && !env.isBlank()) {
            for (String n : env.split(",")) {
                String t = n.trim().toUpperCase(Locale.ROOT);
                if (!t.isEmpty()) out.add(t);
            }
        } else {
            out.addAll(Arrays.asList(DEFAULT_ALLOWED));
        }
        return Collections.unmodifiableSet(out);
    }

    /** @return true when the name is permitted; false means the caller gets a 403. */
    static boolean allowed(String function) {
        return function != null && ALLOWED_FUNCTIONS.contains(function.trim().toUpperCase(Locale.ROOT));
    }

    public static void main(String[] args) throws Exception {
        Environment.registerDestinationDataProvider(PROVIDER);

        HttpServer server = HttpServer.create(new InetSocketAddress(HOST, PORT), 0);
        server.setExecutor(Executors.newFixedThreadPool(8));
        server.createContext("/api/health", SapJcoRest::health);
        server.createContext("/api/table/read", SapJcoRest::tableRead);
        server.createContext("/api/rfc/call", SapJcoRest::rfcCall);
        server.createContext("/api/rfc/batch", SapJcoRest::rfcBatch);
        server.start();
        System.out.println("[jco-rest] SyntaAI JCo REST service listening on http://" + HOST + ":" + PORT);
        System.out.println("[jco-rest] allow-list (" + ALLOWED_FUNCTIONS.size() + "): " + String.join(",", ALLOWED_FUNCTIONS));
    }

    // ---- Handlers ------------------------------------------------------------

    static void health(HttpExchange ex) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("status", "ok");
        r.put("service", "syntaai-jco-service");
        send(ex, 200, r);
    }

    @SuppressWarnings("unchecked")
    static void tableRead(HttpExchange ex) {
        try {
            Map<String, Object> body = (Map<String, Object>) Json.parse(readBody(ex));
            Map<String, String> conn = asStringMap(body.get("connection"));
            String table = str(body.get("table"));
            if (table == null || table.isBlank()) { error(ex, 400, "'table' is required"); return; }

            List<String> fields = new ArrayList<>();
            Object f = body.get("fields");
            if (f instanceof List) for (Object o : (List<Object>) f) if (o != null) fields.add(o.toString());
            String where = str(body.get("where"));
            int maxRows = body.get("maxRows") instanceof Number ? ((Number) body.get("maxRows")).intValue() : 0;

            Map<String, Object> result = doTableRead(conn, table, fields, where, maxRows);
            send(ex, 200, result);
        } catch (Exception e) {
            log("table/read", e);
            error(ex, 500, e.getMessage());
        }
    }

    @SuppressWarnings("unchecked")
    static void rfcCall(HttpExchange ex) {
        try {
            Map<String, Object> body = (Map<String, Object>) Json.parse(readBody(ex));
            Map<String, String> conn = asStringMap(body.get("connection"));
            String function = str(body.get("function"));
            if (function == null || function.isBlank()) { error(ex, 400, "'function' is required"); return; }
            if (!allowed(function)) {
                error(ex, 403, "Function module not permitted by this bridge: " + function);
                return;
            }
            Map<String, Object> params = body.get("parameters") instanceof Map
                    ? (Map<String, Object>) body.get("parameters") : new LinkedHashMap<>();

            Map<String, Object> result = doRfcCall(conn, function, params);
            send(ex, 200, result);
        } catch (IllegalArgumentException e) {
            // A parameter the caller sent could not be bound. This is the
            // caller's bug, and it must never be silently ignored: a BAPI that
            // runs with its input missing reports success while doing nothing.
            log("rfc/call bad-parameter", e);
            error(ex, 400, e.getMessage());
        } catch (Exception e) {
            log("rfc/call", e);
            error(ex, 500, e.getMessage());
        }
    }

    /**
     * POST /api/rfc/batch
     *   {"connection":{...},"calls":[{"function":"..","params":{..}}, ...]}
     *
     * Runs every call in order on one destination inside JCoContext.begin/end, so
     * a stateful sequence (BAPI_XMI_LOGON -> CCMS reads -> BAPI_XMI_LOGOFF) holds
     * its session across calls. The allow-list applies to every entry, and the
     * whole batch is rejected before anything executes if any name fails it.
     *
     * On failure the index is returned with the results collected so far. If an
     * XMI logon had succeeded, BAPI_XMI_LOGOFF is still attempted first so a
     * failed batch does not strand a session on the SAP side.
     */
    @SuppressWarnings("unchecked")
    static void rfcBatch(HttpExchange ex) {
        try {
            Map<String, Object> body = (Map<String, Object>) Json.parse(readBody(ex));
            Map<String, String> conn = asStringMap(body.get("connection"));
            Object rawCalls = body.get("calls");
            if (!(rawCalls instanceof List) || ((List<Object>) rawCalls).isEmpty()) {
                error(ex, 400, "'calls' must be a non-empty array");
                return;
            }
            List<Object> calls = (List<Object>) rawCalls;

            // Validate the whole batch before executing any of it.
            List<String> names = new ArrayList<>();
            for (Object o : calls) {
                if (!(o instanceof Map)) { error(ex, 400, "each entry of 'calls' must be an object"); return; }
                String fnName = str(((Map<String, Object>) o).get("function"));
                if (fnName == null || fnName.isBlank()) { error(ex, 400, "each call needs a 'function'"); return; }
                if (!allowed(fnName)) {
                    error(ex, 403, "Function module not permitted by this bridge: " + fnName);
                    return;
                }
                names.add(fnName.trim().toUpperCase(Locale.ROOT));
            }

            JCoDestination d = dest(conn);
            List<Object> results = new ArrayList<>();
            int failedIndex = -1;
            String failedMessage = null;
            String xmiInterface = null;   // non-null once an XMI logon has succeeded

            JCoContext.begin(d);
            try {
                for (int i = 0; i < calls.size(); i++) {
                    Map<String, Object> call = (Map<String, Object>) calls.get(i);
                    Object rawParams = call.get("params");
                    if (rawParams == null) rawParams = call.get("parameters");
                    Map<String, Object> params = rawParams instanceof Map
                            ? (Map<String, Object>) rawParams : new LinkedHashMap<>();
                    String fnName = names.get(i);
                    try {
                        Map<String, Object> one = runFunction(d, fnName, params);
                        results.add(one);
                        if ("BAPI_XMI_LOGON".equals(fnName)) {
                            Object iface = params.get("INTERFACE");
                            xmiInterface = iface == null ? "XAL" : iface.toString();
                        } else if ("BAPI_XMI_LOGOFF".equals(fnName)) {
                            xmiInterface = null;
                        }
                    } catch (Exception e) {
                        failedIndex = i;
                        failedMessage = fnName + ": " + e.getMessage();
                        break;
                    }
                }
            } finally {
                if (xmiInterface != null) {
                    try {
                        Map<String, Object> off = new LinkedHashMap<>();
                        off.put("INTERFACE", xmiInterface);
                        runFunction(d, "BAPI_XMI_LOGOFF", off);
                    } catch (Exception e) {
                        log("rfc/batch xmi-logoff", e);
                    }
                }
                JCoContext.end(d);
            }

            Map<String, Object> r = new LinkedHashMap<>();
            r.put("results", results);
            r.put("count", results.size());
            if (failedIndex >= 0) {
                r.put("error", failedMessage);
                r.put("failedIndex", failedIndex);
                r.put("success", false);
                send(ex, 500, r);
            } else {
                r.put("success", true);
                send(ex, 200, r);
            }
        } catch (IllegalArgumentException e) {
            log("rfc/batch bad-parameter", e);
            error(ex, 400, e.getMessage());
        } catch (Exception e) {
            log("rfc/batch", e);
            error(ex, 500, e.getMessage());
        }
    }

    // ---- JCo logic -----------------------------------------------------------

    static JCoDestination dest(Map<String, String> conn) throws JCoException {
        String name = PROVIDER.addOrUpdate(conn);
        return JCoDestinationManager.getDestination(name);
    }

    static Map<String, Object> doTableRead(Map<String, String> conn, String table,
                                           List<String> fields, String where, int maxRows) throws JCoException {
        JCoDestination d = dest(conn);
        JCoFunction fn = d.getRepository().getFunction("RFC_READ_TABLE");
        if (fn == null) throw new RuntimeException("RFC_READ_TABLE not found on target system");

        fn.getImportParameterList().setValue("QUERY_TABLE", table);
        fn.getImportParameterList().setValue("DELIMITER", "|");
        if (maxRows > 0) fn.getImportParameterList().setValue("ROWCOUNT", maxRows);

        if (fields != null && !fields.isEmpty()) {
            JCoTable ft = fn.getTableParameterList().getTable("FIELDS");
            for (String fld : fields) { ft.appendRow(); ft.setValue("FIELDNAME", fld.trim()); }
        }
        if (where != null && !where.isBlank()) {
            JCoTable opts = fn.getTableParameterList().getTable("OPTIONS");
            for (String chunk : chunk72(where)) { opts.appendRow(); opts.setValue("TEXT", chunk); }
        }

        fn.execute(d);

        JCoTable fres = fn.getTableParameterList().getTable("FIELDS");
        List<String> cols = new ArrayList<>();
        for (int i = 0; i < fres.getNumRows(); i++) { fres.setRow(i); cols.add(fres.getString("FIELDNAME").trim()); }

        JCoTable data = fn.getTableParameterList().getTable("DATA");
        List<Object> rows = new ArrayList<>();
        for (int i = 0; i < data.getNumRows(); i++) {
            data.setRow(i);
            String line = data.getString("WA");
            String[] vals = line.split("\\|", -1);
            Map<String, Object> row = new LinkedHashMap<>();
            for (int j = 0; j < cols.size(); j++) row.put(cols.get(j), j < vals.length ? vals[j].trim() : "");
            rows.add(row);
        }
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("rows", rows);
        r.put("count", rows.size());
        return r;
    }

    static Map<String, Object> doRfcCall(Map<String, String> conn, String function,
                                         Map<String, Object> params) throws JCoException {
        return runFunction(dest(conn), function, params);
    }

    /**
     * Execute one function module on an already-resolved destination.
     *
     * Split out of doRfcCall so /api/rfc/batch can run a whole sequence on ONE
     * destination inside a single JCoContext, which is what makes stateful
     * protocols such as CCMS/XAL work: BAPI_XMI_LOGON establishes a session that
     * only survives if every following call lands in the same ABAP session.
     */
    static Map<String, Object> runFunction(JCoDestination d, String function,
                                           Map<String, Object> params) throws JCoException {
        JCoFunction fn = d.getRepository().getFunction(function);
        if (fn == null) throw new RuntimeException("Function module not found: " + function);

        List<String> bound = bindImports(fn, params);

        fn.execute(d);

        // export section: scalar fields + flattened structure subfields + export-tables
        Map<String, Object> export = new LinkedHashMap<>();
        collectInto(export, fn.getExportParameterList());
        collectInto(export, fn.getChangingParameterList());

        // tables section: table parameters
        Map<String, Object> tables = new LinkedHashMap<>();
        JCoParameterList tp = fn.getTableParameterList();
        if (tp != null) {
            for (int i = 0; i < tp.getFieldCount(); i++) {
                JCoField fld = tp.getField(i);
                if (fld.isTable()) tables.put(fld.getName(), tableToList(fld.getTable()));
            }
        }

        Map<String, Object> r = new LinkedHashMap<>();
        r.put("export", export);
        r.put("tables", tables);
        r.put("bound", bound);
        return r;
    }

    /**
     * Bind import parameters by what the function module actually declares.
     *
     * Scalars behave exactly as before. Structures accept a JSON object and
     * tables accept a JSON array of objects. A structure-typed importing parameter
     * (as the CCMS monitor calls use) and a table parameter are both bound here,
     * which the previous flat loop silently dropped.
     *
     * Nothing is swallowed. A parameter that cannot be bound throws, so a write
     * cannot execute with its input missing and still report success.
     *
     * @return the names actually bound, echoed to the caller as "bound"
     */
    @SuppressWarnings("unchecked")
    static List<String> bindImports(JCoFunction fn, Map<String, Object> params) {
        List<String> bound = new ArrayList<>();
        if (params == null || params.isEmpty()) return bound;

        JCoParameterList imp = fn.getImportParameterList();
        JCoParameterList tbl = fn.getTableParameterList();

        for (Map.Entry<String, Object> e : params.entrySet()) {
            String name = e.getKey();
            Object val = e.getValue();

            // ---- table import: [ {FIELD: value, ...}, ... ] ----
            if (val instanceof List && hasField(tbl, name)) {
                JCoTable t = tbl.getTable(name);
                for (Object row : (List<Object>) val) {
                    if (!(row instanceof Map))
                        throw new IllegalArgumentException(
                                "Parameter '" + name + "' is a table; every row must be an object, got: " + row);
                    t.appendRow();
                    for (Map.Entry<String, Object> f : ((Map<String, Object>) row).entrySet()) {
                        try {
                            t.setValue(f.getKey(), f.getValue() == null ? null : f.getValue().toString());
                        } catch (Exception ex) {
                            throw new IllegalArgumentException(
                                    "Table '" + name + "' has no field '" + f.getKey() + "': " + ex.getMessage());
                        }
                    }
                }
                bound.add(name);
                continue;
            }

            // ---- structure import: { FIELD: value, ... } ----
            if (val instanceof Map && hasField(imp, name)) {
                JCoStructure st;
                try {
                    st = imp.getStructure(name);
                } catch (Exception ex) {
                    throw new IllegalArgumentException(
                            "Parameter '" + name + "' is not a structure: " + ex.getMessage());
                }
                for (Map.Entry<String, Object> f : ((Map<String, Object>) val).entrySet()) {
                    try {
                        st.setValue(f.getKey(), f.getValue() == null ? null : f.getValue().toString());
                    } catch (Exception ex) {
                        throw new IllegalArgumentException(
                                "Structure '" + name + "' has no field '" + f.getKey() + "': " + ex.getMessage());
                    }
                }
                bound.add(name);
                continue;
            }

            // ---- scalar: unchanged behaviour ----
            if (hasField(imp, name)) {
                try {
                    if (val instanceof Number) imp.setValue(name, ((Number) val).intValue());
                    else if (val != null) imp.setValue(name, val.toString());
                    else imp.setValue(name, (String) null);
                } catch (Exception ex) {
                    throw new IllegalArgumentException(
                            "Could not set '" + name + "': " + ex.getMessage());
                }
                bound.add(name);
                continue;
            }

            throw new IllegalArgumentException(
                    "Function " + fn.getName() + " does not declare an import or table parameter named '"
                            + name + "'");
        }
        return bound;
    }

    /** True when the parameter list declares a field of this name. */
    static boolean hasField(JCoParameterList list, String name) {
        if (list == null || name == null) return false;
        try {
            for (int i = 0; i < list.getFieldCount(); i++) {
                if (name.equals(list.getField(i).getName())) return true;
            }
        } catch (Exception ignore) { }
        return false;
    }

    static void collectInto(Map<String, Object> target, JCoParameterList list) {
        if (list == null) return;
        for (int i = 0; i < list.getFieldCount(); i++) {
            JCoField fld = list.getField(i);
            if (fld.isTable()) {
                target.put(fld.getName(), tableToList(fld.getTable()));
            } else if (fld.isStructure()) {
                JCoStructure s = fld.getStructure();
                Map<String, Object> sm = structToMap(s);
                target.put(fld.getName(), sm);   // keep nested
                target.putAll(sm);               // and flatten subfields (tools read export.get("FQHN"))
            } else {
                target.put(fld.getName(), fld.getString());
            }
        }
    }

    static Map<String, Object> structToMap(JCoStructure s) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < s.getFieldCount(); i++) { JCoField f = s.getField(i); m.put(f.getName(), f.getString()); }
        return m;
    }

    static List<Object> tableToList(JCoTable t) {
        List<Object> list = new ArrayList<>();
        for (int r = 0; r < t.getNumRows(); r++) {
            t.setRow(r);
            Map<String, Object> row = new LinkedHashMap<>();
            for (int c = 0; c < t.getFieldCount(); c++) { JCoField f = t.getField(c); row.put(f.getName(), f.getString()); }
            list.add(row);
        }
        return list;
    }

    static List<String> chunk72(String where) {
        List<String> out = new ArrayList<>();
        for (int p = 0; p < where.length(); p += 72) out.add(where.substring(p, Math.min(p + 72, where.length())));
        return out;
    }

    // ---- HTTP helpers --------------------------------------------------------

    static String readBody(HttpExchange ex) throws Exception {
        return new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
    }

    static void send(HttpExchange ex, int code, Object payload) {
        try {
            byte[] b = Json.write(payload).getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().add("Content-Type", "application/json");
            ex.sendResponseHeaders(code, b.length);
            try (OutputStream os = ex.getResponseBody()) { os.write(b); }
        } catch (Exception e) {
            System.err.println("[jco-rest] send failed: " + e);
        } finally { ex.close(); }
    }

    static void error(HttpExchange ex, int code, String msg) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("error", msg == null ? "unknown error" : msg);
        r.put("success", false);
        send(ex, code, r);
    }

    static void log(String where, Exception e) {
        System.err.println("[jco-rest] " + where + " failed: " + e);
    }

    @SuppressWarnings("unchecked")
    static Map<String, String> asStringMap(Object o) {
        Map<String, String> m = new LinkedHashMap<>();
        if (o instanceof Map) for (Map.Entry<String, Object> e : ((Map<String, Object>) o).entrySet())
            m.put(e.getKey(), e.getValue() == null ? "" : e.getValue().toString());
        return m;
    }

    static String str(Object o) { return o == null ? null : o.toString(); }

    // ---- Dynamic JCo destination provider (per-connection) -------------------

    static final class DynamicProvider implements DestinationDataProvider {
        private final ConcurrentHashMap<String, Properties> dests = new ConcurrentHashMap<>();
        private DestinationDataEventListener listener;

        String addOrUpdate(Map<String, String> conn) {
            String key = String.join("|",
                    conn.getOrDefault("ashost", ""), conn.getOrDefault("sysnr", "00"),
                    conn.getOrDefault("client", "100"), conn.getOrDefault("user", ""));
            String name = "DYN_" + Integer.toHexString(key.hashCode());
            Properties p = new Properties();
            p.setProperty(DestinationDataProvider.JCO_ASHOST, conn.getOrDefault("ashost", ""));
            p.setProperty(DestinationDataProvider.JCO_SYSNR, conn.getOrDefault("sysnr", "00"));
            p.setProperty(DestinationDataProvider.JCO_CLIENT, conn.getOrDefault("client", "100"));
            p.setProperty(DestinationDataProvider.JCO_USER, conn.getOrDefault("user", ""));
            p.setProperty(DestinationDataProvider.JCO_PASSWD, conn.getOrDefault("passwd", ""));
            p.setProperty(DestinationDataProvider.JCO_LANG, conn.getOrDefault("lang", "EN"));
            p.setProperty(DestinationDataProvider.JCO_POOL_CAPACITY, "5");
            p.setProperty(DestinationDataProvider.JCO_PEAK_LIMIT, "10");
            Properties prev = dests.get(name);
            if (prev == null || !prev.equals(p)) {
                dests.put(name, p);
                if (listener != null) listener.updated(name);
            }
            return name;
        }

        public Properties getDestinationProperties(String name) { return dests.get(name); }
        public void setDestinationDataEventListener(DestinationDataEventListener l) { this.listener = l; }
        public boolean supportsEvents() { return true; }
    }

    // ---- Minimal JSON (parse + serialize, RFC 8259) --------------------------

    static final class Json {
        private final String s; private int i;
        private Json(String s) { this.s = s; }

        static Object parse(String s) { Json j = new Json(s); j.ws(); Object v = j.value(); return v; }

        private void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }

        private Object value() {
            ws();
            char c = s.charAt(i);
            switch (c) {
                case '{': return obj();
                case '[': return arr();
                case '"': return str();
                case 't': i += 4; return Boolean.TRUE;
                case 'f': i += 5; return Boolean.FALSE;
                case 'n': i += 4; return null;
                default: return num();
            }
        }

        private Map<String, Object> obj() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++; ws();
            if (s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws(); String k = str(); ws(); i++; /* colon */
                m.put(k, value()); ws();
                char c = s.charAt(i++);
                if (c == '}') break;
            }
            return m;
        }

        private List<Object> arr() {
            List<Object> a = new ArrayList<>();
            i++; ws();
            if (s.charAt(i) == ']') { i++; return a; }
            while (true) {
                a.add(value()); ws();
                char c = s.charAt(i++);
                if (c == ']') break;
            }
            return a;
        }

        private String str() {
            StringBuilder b = new StringBuilder();
            i++; // opening quote
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') break;
                if (c == '\\') {
                    char e = s.charAt(i++);
                    switch (e) {
                        case '"': b.append('"'); break;
                        case '\\': b.append('\\'); break;
                        case '/': b.append('/'); break;
                        case 'b': b.append('\b'); break;
                        case 'f': b.append('\f'); break;
                        case 'n': b.append('\n'); break;
                        case 'r': b.append('\r'); break;
                        case 't': b.append('\t'); break;
                        case 'u': b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
                        default: b.append(e);
                    }
                } else b.append(c);
            }
            return b.toString();
        }

        private Object num() {
            int start = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            String n = s.substring(start, i);
            if (n.indexOf('.') >= 0 || n.indexOf('e') >= 0 || n.indexOf('E') >= 0) return Double.parseDouble(n);
            try { return Long.parseLong(n); } catch (Exception e) { return Double.parseDouble(n); }
        }

        static String write(Object o) { StringBuilder b = new StringBuilder(); writeVal(b, o); return b.toString(); }

        private static void writeVal(StringBuilder b, Object o) {
            if (o == null) b.append("null");
            else if (o instanceof String) writeStr(b, (String) o);
            else if (o instanceof Map) {
                b.append('{'); boolean first = true;
                for (Map.Entry<?, ?> e : ((Map<?, ?>) o).entrySet()) {
                    if (!first) b.append(','); first = false;
                    writeStr(b, String.valueOf(e.getKey())); b.append(':'); writeVal(b, e.getValue());
                }
                b.append('}');
            } else if (o instanceof List) {
                b.append('['); boolean first = true;
                for (Object e : (List<?>) o) { if (!first) b.append(','); first = false; writeVal(b, e); }
                b.append(']');
            } else if (o instanceof Boolean || o instanceof Number) b.append(o.toString());
            else writeStr(b, o.toString());
        }

        private static void writeStr(StringBuilder b, String s) {
            b.append('"');
            for (int k = 0; k < s.length(); k++) {
                char c = s.charAt(k);
                switch (c) {
                    case '"': b.append("\\\""); break;
                    case '\\': b.append("\\\\"); break;
                    case '\b': b.append("\\b"); break;
                    case '\f': b.append("\\f"); break;
                    case '\n': b.append("\\n"); break;
                    case '\r': b.append("\\r"); break;
                    case '\t': b.append("\\t"); break;
                    default:
                        if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                        else b.append(c);
                }
            }
            b.append('"');
        }
    }
}
