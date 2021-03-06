import gdb
import gdbutils
import ngxlua

typ = gdbutils.typ
null = gdbutils.null
newval = gdbutils.newval
ptr2int = gdbutils.ptr2int
err = gdbutils.err
out = gdbutils.out
warn = gdbutils.warn

FRAME_LUA = 0
FRAME_C = 1
FRAME_CONT = 2
FRAME_VARG = 3

FRAME_TYPE = 3
FRAME_P = 4
FRAME_TYPEP = FRAME_TYPE | FRAME_P

CFRAME_RESUME = 1
CFRAME_UNWIND_FF = 2
CFRAME_RAWMASK = ~(CFRAME_RESUME|CFRAME_UNWIND_FF)
CFRAME_OFS_L = 416
CFRAME_OFS_PC = 412

cfunc_cache = {}

LJ_VMST_INTERP = 0
LJ_VMST_C = 1
LJ_VMST_GC = 2
LJ_VMST_EXIT = 3
LJ_VMST_RECORD = 4
LJ_VMST_OPT = 5
LJ_VMST_ASM = 6
LJ_VMST__MAX = 7

vmstates = ['Interpreted', 'C code from intperpreted Lua', \
        'Garbage collector', 'Trace exit handler', \
        'Trace recorder', 'Optimizer', 'Assembler']

NO_BCPOS = ~0

FF_LUA = 0
FF_C   = 1

def get_global_L():
    gL, _ = gdb.lookup_symbol("globalL")
    if gL:
        return gL.value()

    cycle = gdb.lookup_global_symbol("ngx_cycle")
    if cycle:
        cycle = cycle.value()
        gL = ngxlua.ngx_lua_get_main_lua_vm(cycle)
        if gL:
            return gL

    raise gdb.GdbError("No global L located (tried globalL and ngx_cycle)")

def get_cur_L():
    mL = get_global_L()
    return gcref(G(mL)['cur_L'])['th'].address

def cframe_pc(cf):
    #print("CFRAME!!")
    return mref((cf.cast(typ("char*")) + CFRAME_OFS_PC).cast(typ("MRef*")), \
                "BCIns")

def cframe_L(cf):
    return gcref((cf.cast(typ("char*")) + CFRAME_OFS_L) \
            .cast(typ("GCRef*")).dereference())['th'].address

def frame_ftsz(tv):
    return tv['fr']['tp']['ftsz']

def frame_type(f):
    return (frame_ftsz(f) & FRAME_TYPE)

def frame_islua(f):
    return frame_type(f) == FRAME_LUA

def frame_typep(f):
    return (frame_ftsz(f) & FRAME_TYPEP)

def frame_isvarg(f):
    return frame_typep(f) == FRAME_VARG

def frame_iscont(f):
    return frame_typep(f) == FRAME_CONT

def sizeof(typ):
    return gdb.parse_and_eval("sizeof(" + typ + ")")

def gcref(r):
    return r['gcptr32'].cast(typ("uintptr_t")).cast(typ("GCobj*"))

def frame_gc(frame):
    return gcref(frame['fr']['func'])

def obj2gco(v):
    return v.cast(typ("GCobj*"))

def mref(r, t):
    return r['ptr32'].cast(typ("uintptr_t")).cast(typ(t + "*"))

def frame_pc(f):
    return mref(f['fr']['tp']['pcr'], "BCIns")

def frame_contpc(f):
    return frame_pc(f - 1)

def bc_a(i):
    return newval("BCReg", (i >> 8) & 0xff)

def frame_prevl(f):
    return f - (1 + bc_a(frame_pc(f)[-1]))

def frame_sized(f):
    return (frame_ftsz(f) & ~FRAME_TYPEP)

def frame_prevd(f):
    #print "f = %x, sized = %x" % (ptr2int(f.cast(typ("char*"))), frame_sized(f))
    return (f.cast(typ("char*")) - frame_sized(f)).cast(typ("TValue*"))

def tvref(r):
    return mref(r, "TValue")

def lj_debug_frame(L, base, level, bot):
    frame = base - 1
    nextframe = frame
    while frame > bot:
        #print "checking level %d\n" % level
        if frame_gc(frame) == obj2gco(L):
            level += 1

        if level == 0:
            return (frame, nextframe - frame)

        level -= 1
        nextframe = frame
        if frame_islua(frame):
            frame = frame_prevl(frame)
        else:
            if frame_isvarg(frame):
                level += 1
            frame = frame_prevd(frame)
    return (null(), level)

def frame_func(f):
    return frame_gc(f)['fn'].address

def isluafunc(fn):
    return fn['c']['ffid'] == FF_LUA

def isffunc(fn):
    return fn['c']['ffid'] > FF_C

def funcproto(fn):
    return (mref(fn['l']['pc'], "char") - typ("GCproto").sizeof) \
            .cast(typ("GCproto*"))

def proto_bc(pt):
    return (pt.cast(typ("char*")) + typ("GCproto").sizeof).cast(typ("BCIns*"))

def proto_bcpos(pt, pc):
    return (pc - proto_bc(pt)).cast(typ("BCPos"))

def proto_lineinfo(pt):
    return mref(pt['lineinfo'], "void")

def lj_debug_line(pt, pc):
    lineinfo = proto_lineinfo(pt)
    if pc <= pt['sizebc'] and lineinfo:
        first = pt['firstline']
        if pc == pt['sizebc']:
            return first + pt['numline']
        if pc == 0:
            return first
        pc -= 1
        if pt['numline'] < 256:
            return first + lineinfo.cast(typ("uint8_t*"))[pc].cast(typ("BCLine"))
        elif pt['numline'] < 65536:
            return first + lineinfo.cast(typ("uint16_t*"))[pc].cast(typ("BCLine"))
        else:
            return first + lineinfo.cast(typ("uint32_t*"))[pc].cast(typ("BCLine"))
    return 0

def debug_framepc(L, T, fn, pt, nextframe):
    if not isluafunc(fn):
        return NO_BCPOS
    if not nextframe:
        cf = cframe_raw(L['cframe'])
        if not cf or cframe_pc(cf) == cframe_L(cf):
            return NO_BCPOS
        ins = cframe_pc(cf)
    else:
        if frame_islua(nextframe):
            ins = frame_pc(nextframe)
        elif frame_iscont(nextframe):
            ins = frame_contpc(nextframe)
        else:
            warn("Lua function below errfunc/gc/hook not supported yet")
            return NO_BCPOS
    pos = proto_bcpos(pt, ins) - 1
    if pos > pt['sizebc']:
        if not T:
            # TODO
            #T = ((ins - 1).cast(typ("char*")) - \
                    #typ("GCtrace")['startins'].bitpos / 8).cast(typ("GCtrace*"))
            return NO_BCPOS
        pos = proto_bcpos(pt, mref(T['startpc'], "BCIns"))
    return pos

def debug_frameline(L, T, fn, pt, nextframe):
    pc = debug_framepc(L, T, fn, pt, nextframe)
    if pc != NO_BCPOS:
        pt = funcproto(fn)
        return lj_debug_line(pt, pc)
    return -1

def strref(r):
    return gcref(r)['str'].address

def proto_chunkname(pt):
    return strref(pt['chunkname'])

def strdata(s):
    return (s + 1).cast(typ("char*"))

def G(L):
    return mref(L['glref'], "global_State")

def cframe_raw(cf):
    return (cf.cast(typ("intptr_t")) & CFRAME_RAWMASK).cast(typ("void*"))

def lj_debug_dumpstack(L, T, depth, base):
    global cfunc_cache

    level = 0
    dir = 1
    if depth < 0:
        level = ~depth
        depth = dir = -1

    bot = tvref(L['stack'])
    bt = ""
    while level != depth:
        #print "checking level: %d" % level

        frame, size = lj_debug_frame(L, base, level, bot)

        if frame:
            nextframe = (frame + size) if size else null()
            fn = frame_func(frame)
            #print "type(fn) == %s" % fn.type
            if not fn:
                return ""

            if isluafunc(fn):
                pt = funcproto(fn)
                line = debug_frameline(L, T, fn, pt, nextframe)
                if line < 0:
                    #print str(pt.dereference)
                    line = int(pt['firstline'])
                name = proto_chunkname(pt)
                if not name:
                    return ""
                s = strdata(name)
                path = s.string('iso-8859-6', 'ignore', name['len'])
                bt += "%s:%d\n" % (path, line)

            elif isffunc(fn):
                bt += "builtin#%d\n" % int(fn['c']['ffid'])

            else:
                cfunc = fn['c']['f']
                key = str(cfunc)
                if key in cfunc_cache:
                    sym = cfunc_cache[key]

                else:
                    sym = "C:%s\n" % cfunc
                    m = re.search('<.*?(\w+)*.*?>', cfunc.__str__())
                    if m:
                        sym = "C:%s\n" % m.group(1)
                    else:
                        sym = "C:%s\n" % key

                    cfunc_cache[key] = sym

                bt += sym
                #print "bt: " + sym

        elif dir == 1:
            break

        else:
            level -= size

        level += dir

    return bt

def G2GG(gl):
    return (gl.cast(typ("char*")) - typ("GG_State")['g'].bitpos / 8) \
            .cast(typ("GG_State*"))

def G2J(gl):
    return G2GG(gl)['J'].address

def traceref(J, n):
    return gcref(J['trace'][n]).cast(typ("GCtrace*"))

class lbt(gdb.Command):
    """This command dumps out the current Lua-land backtrace in the lua_State specified. Only LuaJIT 2.1 is supported.
Usage: lbt [L]"""

    def __init__ (self):
        super (lbt, self).__init__("lbt", gdb.COMMAND_USER)

    def invoke (self, args, from_tty):
        argv = gdb.string_to_argv(args)
        if len(argv) > 1:
            raise gdb.GdbError("Usage: lbt [L]")

        if len(argv) == 1:
            L = gdbutils.parse_ptr(argv[0], "lua_State*")
            if not L or str(L) == "void":
                raise gdb.GdbError("L empty")
        else:
            L = get_cur_L()

        #print "g: ", hex(int(L['glref']['ptr32']))

        g = G(L)

        vmstate = int(g['vmstate'])
        #print "vmstate = %d" % vmstate

        if vmstate >= 0:
            #print "compiled code"
            traceno = vmstate
            J = G2J(g)
            T = traceref(J, traceno)
            base = tvref(g['jit_base'])
            if not base:
                raise gdb.GdbError("jit base is NULL")
            bt = lj_debug_dumpstack(L, T, 30, base)

        else:
            if vmstate == ~LJ_VMST_INTERP and not L['cframe']:
                out("No Lua code running.\n")
                return

            if vmstate == ~LJ_VMST_INTERP or \
                   vmstate == ~LJ_VMST_C or \
                   vmstate == ~LJ_VMST_GC:
                base = L['base']
                bt = lj_debug_dumpstack(L, 0, 30, base)

            else:
                out("No Lua code running.\n")
                return
        if not bt:
            out("Empty backtrace.\n")
        out(bt)

lbt()

class lvmst(gdb.Command):
    """This command prints out the current LuaJIT VM state in the lua_State specified.
Usage: lvmst [L]"""

    def __init__ (self):
        super (lvmst, self).__init__("lvmst", gdb.COMMAND_USER)

    def invoke (self, args, from_tty):
        argv = gdb.string_to_argv(args)
        if len(argv) > 1:
            err("Usage: lvmst [L]")

        if len(argv) == 1:
            L = gdbutils.parse_ptr(argv[0], "lua_State*")
            if not L or str(L) == "void":
                raise gdb.GdbError("L empty")
        else:
            L = get_cur_L()

        #print "g: ", hex(int(L['glref']['ptr32']))

        g = G(L)

        vmstate = int(g['vmstate'])
        if vmstate >= 0:
            out("Compiled (trace #%d)\n" % vmstate)

        elif ~vmstate >= LJ_VMST__MAX:
            raise gdb.GdbError("Invalid VM state: ", ~vmstate)

        else:
            #print "vmstate = %d" % vmstate
            out("current VM state: %s\n" % vmstates[~vmstate])

lvmst()
