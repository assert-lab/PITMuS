import javax.tools.*;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;

// A persistent in-JVM batch compiler. Reads "id<TAB>srcPath<TAB>outDir" lines
// from stdin, compiles each single file against a fixed classpath with
// `--release 8`, and prints "id<TAB>OK" or "id<TAB>ERR<TAB>firstErrorMessage".
// Reuses one JVM (and its cached platform classpath) for the whole batch, so
// javac startup is paid once instead of once per mutant.
public class PitmusCompile {
    public static void main(String[] a) throws Exception {
        String cp = a[0];
        int workers = Integer.parseInt(a[1]);
        final JavaCompiler comp = ToolProvider.getSystemJavaCompiler();
        if (comp == null) { System.err.println("NO_COMPILER"); System.exit(2); }
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, "UTF-8"));
        final List<String[]> reqs = new ArrayList<>();
        String line;
        while ((line = in.readLine()) != null) {
            if (line.isEmpty()) continue;
            String[] p = line.split("\t", 3);
            if (p.length == 3) reqs.add(p);
        }
        final String classpath = cp;
        ExecutorService ex = Executors.newFixedThreadPool(workers);
        List<Future<String>> fs = new ArrayList<>();
        for (final String[] p : reqs) {
            fs.add(ex.submit(new Callable<String>() {
                public String call() {
                    String id = p[0], src = p[1], out = p[2];
                    try {
                        DiagnosticCollector<JavaFileObject> diags = new DiagnosticCollector<>();
                        StandardJavaFileManager fm = comp.getStandardFileManager(diags, null, null);
                        Iterable<? extends JavaFileObject> units = fm.getJavaFileObjects(new File(src));
                        List<String> opts = Arrays.asList("--release", "8", "-cp", classpath, "-d", out);
                        StringWriter sw = new StringWriter();
                        boolean ok = comp.getTask(sw, fm, diags, opts, null, units).call();
                        fm.close();
                        if (ok) return id + "\tOK\t";
                        String msg = "compile failed";
                        for (Diagnostic<?> d : diags.getDiagnostics()) {
                            if (d.getKind() == Diagnostic.Kind.ERROR) {
                                msg = d.getMessage(null).replace('\n', ' ').replace('\t', ' ').replace('\r', ' ');
                                break;
                            }
                        }
                        return id + "\tERR\t" + msg;
                    } catch (Throwable t) {
                        return id + "\tERR\texc: " + String.valueOf(t).replace('\n', ' ').replace('\t', ' ');
                    }
                }
            }));
        }
        StringBuilder sb = new StringBuilder();
        for (Future<String> f : fs) {
            try { sb.append(f.get()).append('\n'); }
            catch (Exception e) { sb.append("?\tERR\tfuture-fail\n"); }
        }
        ex.shutdown();
        System.out.print(sb);
        System.out.flush();
    }
}
