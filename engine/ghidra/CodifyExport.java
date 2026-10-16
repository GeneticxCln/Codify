// Codify's export script for Ghidra's analyzeHeadless. Owned by the engine (engine/binary_analysis.py copies it into a
// private run directory and names it with -postScript); a model never supplies, edits or chooses a script.
//
// It writes ONE bounded JSON document and exits: the program's functions (with callers and callees), imports, defined
// strings and memory blocks. It reads the analysed program and changes nothing in it, opens no socket, and writes only
// the file it is told to. The caps come from the engine as arguments, so they live in one place (Python):
//
//   args: <out path> <max functions> <max imports> <max strings> <max blocks> <max edges per function> <max text chars>
//
// The engine re-bounds and cleans everything it reads back; the caps here keep the document small to begin with.

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Set;

public class CodifyExport extends GhidraScript {

    private static final int VERSION = 1;

    private int maxText = 200;

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 7) {
            throw new IllegalArgumentException("CodifyExport needs 7 arguments, got " + args.length);
        }
        String out = args[0];
        int maxFunctions = Integer.parseInt(args[1]);
        int maxImports = Integer.parseInt(args[2]);
        int maxStrings = Integer.parseInt(args[3]);
        int maxBlocks = Integer.parseInt(args[4]);
        int maxEdges = Integer.parseInt(args[5]);
        maxText = Integer.parseInt(args[6]);

        String json;
        try {
            json = build(maxFunctions, maxImports, maxStrings, maxBlocks, maxEdges);
        } catch (Throwable t) {
            json = "{\"version\":" + VERSION + ",\"error\":" + quote(String.valueOf(t)) + "}";
        }
        Files.write(Paths.get(out), json.getBytes(StandardCharsets.UTF_8));
    }

    private String build(int maxFunctions, int maxImports, int maxStrings, int maxBlocks, int maxEdges) throws Exception {
        StringBuilder sb = new StringBuilder(1 << 16);
        sb.append("{\"version\":").append(VERSION);

        sb.append(",\"program\":{");
        sb.append("\"language\":").append(quote(currentProgram.getLanguageID().getIdAsString()));
        sb.append(",\"compiler\":").append(quote(currentProgram.getCompilerSpec().getCompilerSpecID().getIdAsString()));
        sb.append(",\"format\":").append(quote(currentProgram.getExecutableFormat()));
        sb.append(",\"image_base\":").append(quote(currentProgram.getImageBase().toString()));
        sb.append(",\"bits\":").append(quote(String.valueOf(currentProgram.getDefaultPointerSize() * 8)));
        sb.append("}");

        int functionTotal = 0;
        sb.append(",\"functions\":[");
        FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
        boolean first = true;
        while (functions.hasNext() && !monitor.isCancelled()) {
            Function f = functions.next();
            functionTotal++;
            if (functionTotal > maxFunctions) {
                continue;
            }
            if (!first) {
                sb.append(',');
            }
            first = false;
            sb.append("{\"name\":").append(quote(f.getName()));
            sb.append(",\"address\":").append(quote(f.getEntryPoint().toString()));
            sb.append(",\"size\":").append(f.getBody().getNumAddresses());
            sb.append(",\"thunk\":").append(f.isThunk());
            sb.append(",\"callers\":");
            names(sb, f.getCallingFunctions(monitor), maxEdges);
            sb.append(",\"callees\":");
            names(sb, f.getCalledFunctions(monitor), maxEdges);
            sb.append('}');
        }
        sb.append(']');

        int importTotal = 0;
        sb.append(",\"imports\":[");
        SymbolIterator externals = currentProgram.getSymbolTable().getExternalSymbols();
        first = true;
        while (externals.hasNext() && !monitor.isCancelled()) {
            Symbol s = externals.next();
            importTotal++;
            if (importTotal > maxImports) {
                continue;
            }
            if (!first) {
                sb.append(',');
            }
            first = false;
            sb.append("{\"name\":").append(quote(s.getName()));
            sb.append(",\"library\":").append(quote(s.getParentNamespace().getName())).append('}');
        }
        sb.append(']');

        int stringTotal = 0;
        sb.append(",\"strings\":[");
        DataIterator data = currentProgram.getListing().getDefinedData(true);
        first = true;
        while (data.hasNext() && !monitor.isCancelled()) {
            Data d = data.next();
            if (!d.hasStringValue()) {
                continue;
            }
            stringTotal++;
            if (stringTotal > maxStrings) {
                continue;
            }
            if (!first) {
                sb.append(',');
            }
            first = false;
            Address at = d.getAddress();
            sb.append("{\"address\":").append(quote(at.toString()));
            sb.append(",\"value\":").append(quote(String.valueOf(d.getValue()))).append('}');
        }
        sb.append(']');

        MemoryBlock[] blocks = currentProgram.getMemory().getBlocks();
        sb.append(",\"blocks\":[");
        for (int i = 0; i < blocks.length && i < maxBlocks; i++) {
            MemoryBlock b = blocks[i];
            if (i > 0) {
                sb.append(',');
            }
            sb.append("{\"name\":").append(quote(b.getName()));
            sb.append(",\"start\":").append(quote(b.getStart().toString()));
            sb.append(",\"size\":").append(b.getSize());
            sb.append(",\"read\":").append(b.isRead());
            sb.append(",\"write\":").append(b.isWrite());
            sb.append(",\"execute\":").append(b.isExecute()).append('}');
        }
        sb.append(']');

        sb.append(",\"totals\":{\"functions\":").append(functionTotal);
        sb.append(",\"imports\":").append(importTotal);
        sb.append(",\"strings\":").append(stringTotal);
        sb.append(",\"blocks\":").append(blocks.length).append("}}");
        return sb.toString();
    }

    private void names(StringBuilder sb, Set<Function> set, int max) {
        sb.append('[');
        int n = 0;
        for (Function f : set) {
            if (n >= max) {
                break;
            }
            if (n > 0) {
                sb.append(',');
            }
            sb.append(quote(f.getName()));
            n++;
        }
        sb.append(']');
    }

    /** A JSON string, clipped to the text cap, with every control character escaped. */
    private String quote(String raw) {
        String s = raw == null ? "" : raw;
        if (s.length() > maxText) {
            s = s.substring(0, maxText);
        }
        StringBuilder sb = new StringBuilder(s.length() + 2);
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20 || (c >= 0xD800 && c <= 0xDFFF)) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
        return sb.toString();
    }
}
