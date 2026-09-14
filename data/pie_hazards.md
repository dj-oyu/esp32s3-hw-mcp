# TRM 1.7 Instruction Performance (verbatim, with page markers)
Source: ESP32-S3 TRM v1.8, printed pages 65-75. Printed page == PDF page.

<!-- page 65 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
1.7
Instruction Performance
For processors designed based on a pipeline, it is ideal that CPU issues one instruction onto the pipeline per
processor cycle. The ESP32-S3 Xtensa processor adopts the 5-stage pipeline technology: I (instruction
fetch), R (decode), E (execute), M (memory access), and W (write back). Table 1.7-1 shows what the
processor does at each pipeline stage.
Table 1.7-1. Five-Stage Pipeline of Xtensa Processor
Pipeline Stage
Number
Operation
I
-
Align instructions (24-bit and 32-bit instructions supported)
R
0
Read the general-purpose registers AR and QR
Decode instructions, detect interlocks, and forward operands
E
1
For arithmetic instructions, the ALU (addition, subtraction, multiplication, etc.) works
For read memory instructions, generate virtual addresses for memory access
For branch jump instructions, select jump addresses
M
2
Issue read and write memory accesses
W
3
Write back to registers the calculated results and the data read from memory
The processor cannot issue an instruction to the pipeline until all the operands and hardware resources
required for the operation are ready. However, there are the following hazards in the actual program running
process, which can cause stopped pipeline and delayed implementation of instructions.
1.7.1
Data Hazard
When instruction A writes the result to register X (including explicit general-purpose registers and implicit
special registers), and instruction B needs to use the same register as an input operand, this case is referred
to as that instruction B depends on instruction A. If instruction A prepares the result to be written to register X
at the end of the SA pipeline stage, and instruction B reads the data in register X at the beginning of the SB
pipeline stage, then instruction A must be issued D=max(SA-SB+1, 0) cycles before instruction B.
If the processor fetches instruction B less than D cycles after instruction A, the processor delays issuing
instruction B until D cycles have passed. The act of a processor delaying an instruction because of pipeline
interactions is called an interlock.
Suppose the SA pipeline stage of instruction A is W and the SB pipeline stage of instruction B is E, instruction
B is issued to the pipeline D=max(2-1+1, 0)=2 cycles later than instruction A as shown in Figure 1.7-1.
When the output operand of an instruction is designed to be available at the end of a pipeline stage, it means
that the operation of the instruction is over. Usually, instructions that depend on this result data must wait until
the output operand is written to the corresponding register before retrieving it from the corresponding register.
The Xtensa processor supports the ”bypass” operation. It detects when the input operand of an instruction is
generated at which pipeline stage of the instruction and does not need to wait for the data to be written to the
register. It can directly forward the data from the pipeline stage where it is generated to the stage where it is
needed.
Espressif Systems
65

<!-- page 66 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
Figure 1.7-1. Interlock Caused by Instruction Operand Dependency
Data dependencies between instructions are determined by the dependencies between operands and the
pipeline stage at which reads and writes happen. Table 1.7-2 lists all operands of the ESP32-S3 extended
instructions, including implicit special register write (def) and read (use) pipeline stage information.
Table 1.7-2. Extended Instruction Pipeline Stages
Operand Pipeline Stage
Special Register Pipeline Stage
Instruction
Use
Def
Use
Def
EE.ANDQ
qx 1, qy 1
qa 1
—
—
EE.BITREV
ax 1
qa 1, ax 1
FFT_BIT_WIDTH
1
—
EE.CLR_BIT_GPIO_OUT
—
—
GPIO_OUT 1
GPIO_OUT 1
EE.CMUL.S16
qx 1, qy 1
qz 2
SAR 1
—
EE.CMUL.S16.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qz 2
SAR 1
—
EE.CMUL.S16.ST.INCP
qv 2, as 1, qx 1,
qy 1
as 1, qz 2
SAR 1
—
EE.FFT.AMS.S16.LD.INCP
as 1, qx 1, qy 1,
qm 1
qu 2, as 1, qz 2,
qz1 2
SAR 1
—
EE.FFT.AMS.S16.LD.INCP.UAUP
as 1, qx 1, qy 1,
qm 1
qu 2, as 1, qz 2,
qz1 2
SAR
1,
SAR_BYTE
1, UA_STATE 1
UA_STATE 1
EE.FFT.AMS.S16.LD.R32.DECP
as 1, qx 1, qy 1,
qm 1
qu 2, as 1, qz 2,
qz1 2
SAR 1
—
EE.FFT.AMS.S16.ST.INCP
qv 2, as0 1, as 1,
qx 1, qy 1, qm 1
qz1 2, as0 2, as
1
SAR 1
—
EE.FFT.CMUL.S16.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1, qz 2
SAR 1
—
Espressif Systems
66

<!-- page 67 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
EE.FFT.CMUL.S16.ST.XP
qx 1, qy 1, qv 2,
as 1, ad 1
as 1
SAR 1
—
EE.FFT.R2BF.S16
qx 1, qy 1
qa0 1, qa1 1
—
—
EE.FFT.R2BF.S16.ST.INCP
qx 1, qy 1, as 1
qa0 1, as 1
—
—
EE.FFT.VST.R32.DECP
qv 2, as 1
as 1
—
—
EE.GET_GPIO_IN
—
au 1
GPIO_IN 1
—
EE.LD.128.USAR.IP
as 1
qu 2, as 1
—
SAR_BYTE 1
EE.LD.128.USAR.XP
as 1, ad 1
qu 2, as 1
—
SAR_BYTE 1
EE.LD.ACCX.IP
as 1
as 1
—
ACCX 2
EE.LD.QACC_H.H.32.IP
as 1
as 1
QACC_H 1
QACC_H 2
EE.LD.QACC_H.L.128.IP
as 1
as 1
QACC_H 1
QACC_H 2
EE.LD.QACC_L.H.32.IP
as 1
as 1
QACC_L 1
QACC_L 2
EE.LD.QACC_L.L.128.IP
as 1
as 1
QACC_L 1
QACC_L 2
EE.LD.UA_STATE.IP
as 1
as 1
—
UA_STATE 2
EE.LDF.128.IP
as 1
fu3 2, fu2 2, fu1
2, fu0 2, as 1
—
—
EE.LDF.128.XP
as 1, ad 1
fu3 2, fu2 2, fu1
2, fu0 2, as 1
—
—
EE.LDF.64.IP
as 1
fu1 2, fu0 2, as 1
—
—
EE.LDF.64.XP
as 1, ad 1
fu1 2, fu0 2, as 1
—
—
EE.LDQA.S16.128.IP
as 1
as 1
—
QACC_L
2,
QACC_H 2
EE.LDQA.S16.128.XP
as 1, ad 1
as 1
—
QACC_L
2,
QACC_H 2
EE.LDQA.S8.128.IP
as 1
as 1
—
QACC_L
2,
QACC_H 2
EE.LDQA.S8.128.XP
as 1, ad 1
as 1
—
QACC_L
2,
QACC_H 2
EE.LDQA.U16.128.IP
as 1
as 1
—
QACC_L
2,
QACC_H 2
EE.LDQA.U16.128.XP
as 1, ad 1
as 1
—
QACC_L
2,
QACC_H 2
EE.LDQA.U8.128.IP
as 1
as 1
—
QACC_L
2,
QACC_H 2
EE.LDQA.U8.128.XP
as 1, ad 1
as 1
—
QACC_L
2,
QACC_H 2
EE.LDXQ.32
qs 1, as 1
qu 2
—
—
EE.MOV.S16.QACC
qs 1
—
—
QACC_L
1,
QACC_H 1
EE.MOV.S8.QACC
qs 1
—
—
QACC_L
1,
QACC_H 1
EE.MOV.U16.QACC
qs 1
—
—
QACC_L
1,
QACC_H 1
Espressif Systems
67

<!-- page 68 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
EE.MOV.U8.QACC
qs 1
—
—
QACC_L
1,
QACC_H 1
EE.MOVI.32.A
qs 1
au 1
—
—
EE.MOVI.32.Q
as 1
qu 1
—
—
EE.NOTQ
qx 1
qa 1
—
—
EE.ORQ
qx 1, qy 1
qa 1
—
—
EE.SET_BIT_GPIO_OUT
—
—
GPIO_OUT 1
GPIO_OUT 1
EE.SLCI.2Q
qs1 1, qs0 1
qs1 1, qs0 1
—
—
EE.SLCXXP.2Q
qs1 1, qs0 1, as 1,
ad 1
qs1 1, qs0 1, as 1
—
—
EE.SRC.Q
qs0 1, qs1 1
qa 1
SAR_BYTE 1
—
EE.SRC.Q.LD.IP
as 1, qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE 1
—
EE.SRC.Q.LD.XP
as 1, ad 1, qs0 1,
qs1 1
qu 2, as 1, qs0 1
SAR_BYTE 1
—
EE.SRC.Q.QUP
qs0 1, qs1 1
qa 1, qs0 1
SAR_BYTE 1
—
EE.SRCI.2Q
qs1 1, qs0 1
qs1 1, qs0 1
—
—
EE.SRCMB.S16.QACC
as 1
qu 1
QACC_H
1,
QACC_L 1
QACC_H
1,
QACC_L 1
EE.SRCMB.S8.QACC
as 1
qu 1
QACC_H
1,
QACC_L 1
QACC_H
1,
QACC_L 1
EE.SRCQ.128.ST.INCP
qs0 1, qs1 1, as 1
as 1
SAR_BYTE 1
—
EE.SRCXXP.2Q
qs1 1, qs0 1, as 1,
ad 1
qs1 1, qs0 1, as 1
—
—
EE.SRS.ACCX
as 1
au 1
ACCX 1
ACCX 1
EE.ST.ACCX.IP
as 1
as 1
ACCX 1
—
EE.ST.QACC_H.H.32.IP
as 1
as 1
QACC_H 1
—
EE.ST.QACC_H.L.128.IP
as 1
as 1
QACC_H 1
—
EE.ST.QACC_L.H.32.IP
as 1
as 1
QACC_L 1
—
EE.ST.QACC_L.L.128.IP
as 1
as 1
QACC_L 1
—
EE.ST.UA_STATE.IP
as 1
as 1
UA_STATE 1
—
EE.STF.128.IP
fv3 1, fv2 1, fv1 1,
fv0 1, as 1
as 1
—
—
EE.STF.128.XP
fv3 1, fv2 1, fv1 1,
fv0 1, as 1, ad 1
as 1
—
—
EE.STF.64.IP
fv1 1, fv0 1, as 1
as 1
—
—
EE.STF.64.XP
fv1 1, fv0 1, as 1,
ad 1
as 1
—
—
EE.STXQ.32
qv 1, qs 1, as 1
—
—
—
EE.VADDS.S16
qx 1, qy 1
qa 1
—
—
EE.VADDS.S16.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VADDS.S16.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VADDS.S32
qx 1, qy 1
qa 1
—
—
EE.VADDS.S32.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
Espressif Systems
68

<!-- page 69 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
EE.VADDS.S32.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VADDS.S8
qx 1, qy 1
qa 1
—
—
EE.VADDS.S8.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VADDS.S8.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VCMP.EQ.S16
qx 1, qy 1
qa 1
—
—
EE.VCMP.EQ.S32
qx 1, qy 1
qa 1
—
—
EE.VCMP.EQ.S8
qx 1, qy 1
qa 1
—
—
EE.VCMP.GT.S16
qx 1, qy 1
qa 1
—
—
EE.VCMP.GT.S32
qx 1, qy 1
qa 1
—
—
EE.VCMP.GT.S8
qx 1, qy 1
qa 1
—
—
EE.VCMP.LT.S16
qx 1, qy 1
qa 1
—
—
EE.VCMP.LT.S32
qx 1, qy 1
qa 1
—
—
EE.VCMP.LT.S8
qx 1, qy 1
qa 1
—
—
EE.VLD.128.IP
as 1
qu 2, as 1
—
—
EE.VLD.128.XP
as 1, ad 1
qu 2, as 1
—
—
EE.VLD.H.64.IP
as 1
qu 2, as 1
—
—
EE.VLD.H.64.XP
as 1, ad 1
qu 2, as 1
—
—
EE.VLD.L.64.IP
as 1
qu 2, as 1
—
—
EE.VLD.L.64.XP
as 1, ad 1
qu 2, as 1
—
—
EE.VLDBC.16
as 1
qu 2
—
—
EE.VLDBC.16.IP
as 1
qu 2, as 1
—
—
EE.VLDBC.16.XP
as 1, ad 1
qu 2, as 1
—
—
EE.VLDBC.32
as 1
qu 2
—
—
EE.VLDBC.32.IP
as 1
qu 2, as 1
—
—
EE.VLDBC.32.XP
as 1, ad 1
qu 2, as 1
—
—
EE.VLDBC.8
as 1
qu 2
—
—
EE.VLDBC.8.IP
as 1
qu 2, as 1
—
—
EE.VLDBC.8.XP
as 1, ad 1
qu 2, as 1
—
—
EE.VLDHBC.16.INCP
as 1
qu 2, qu1 2, as 1
—
—
EE.VMAX.S16
qx 1, qy 1
qa 1
—
—
EE.VMAX.S16.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VMAX.S16.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VMAX.S32
qx 1, qy 1
qa 1
—
—
EE.VMAX.S32.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VMAX.S32.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VMAX.S8
qx 1, qy 1
qa 1
—
—
EE.VMAX.S8.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VMAX.S8.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VMIN.S16
qx 1, qy 1
qa 1
—
—
Espressif Systems
69

<!-- page 70 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
EE.VMIN.S16.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VMIN.S16.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VMIN.S32
qx 1, qy 1
qa 1
—
—
EE.VMIN.S32.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VMIN.S32.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VMIN.S8
qx 1, qy 1
qa 1
—
—
EE.VMIN.S8.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VMIN.S8.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VMUL.S16
qx 1, qy 1
qz 2
SAR 1
—
EE.VMUL.S16.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qz 2
SAR 1
—
EE.VMUL.S16.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qz 2
SAR 1
—
EE.VMUL.S8
qx 1, qy 1
qz 2
SAR 1
—
EE.VMUL.S8.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qz 2
SAR 1
—
EE.VMUL.S8.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qz 2
SAR 1
—
EE.VMUL.U16
qx 1, qy 1
qz 2
SAR 1
—
EE.VMUL.U16.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qz 2
SAR 1
—
EE.VMUL.U16.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qz 2
SAR 1
—
EE.VMUL.U8
qx 1, qy 1
qz 2
SAR 1
—
EE.VMUL.U8.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qz 2
SAR 1
—
EE.VMUL.U8.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qz 2
SAR 1
—
EE.VMULAS.S16.ACCX
qx 1, qy 1
—
ACCX 2
ACCX 2
EE.VMULAS.S16.ACCX.LD.IP
as 1, qx 1, qy 1
qu 2, as 1
ACCX 2
ACCX 2
EE.VMULAS.S16.ACCX.LD.IP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
ACCX 2
ACCX 2
EE.VMULAS.S16.ACCX.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1
ACCX 2
ACCX 2
EE.VMULAS.S16.ACCX.LD.XP.QUP
as 1, ad 1, qx 1,
qy 1, qs0 1, qs1
1
qu 2, as 1, qs0 1
SAR_BYTE
1,
ACCX 2
ACCX 2
EE.VMULAS.S16.QACC
qx 1, qy 1
—
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S16.QACC.LD.IP
as 1, qx 1, qy 1
qu 2, as 1
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S16.QACC.LD.IP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
Espressif Systems
70

<!-- page 71 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
EE.VMULAS.S16.QACC.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S16.QACC.LD.XP.QUP
as 1, ad 1, qx 1,
qy 1, qs0 1, qs1
1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S16.QACC.LDBC.INCP
as 1, qx 1, qy 1
qu 2, as 1
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S16.QACC.LDBC.INCP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S8.ACCX
qx 1, qy 1
—
ACCX 2
ACCX 2
EE.VMULAS.S8.ACCX.LD.IP
as 1, qx 1, qy 1
qu 2, as 1
ACCX 2
ACCX 2
EE.VMULAS.S8.ACCX.LD.IP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
ACCX 2
ACCX 2
EE.VMULAS.S8.ACCX.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1
ACCX 2
ACCX 2
EE.VMULAS.S8.ACCX.LD.XP.QUP
as 1, ad 1, qx 1,
qy 1, qs0 1, qs1
1
qu 2, as 1, qs0 1
SAR_BYTE
1,
ACCX 2
ACCX 2
EE.VMULAS.S8.QACC
qx 1, qy 1
—
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VMULAS.S8.QACC.LD.IP
as 1, qx 1, qy 1
qu 2, as 1
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S8.QACC.LD.IP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S8.QACC.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S8.QACC.LD.XP.QUP
as 1, ad 1, qx 1,
qy 1, qs0 1, qs1
1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S8.QACC.LDBC.INCP
as 1, qx 1, qy 1
qu 2, as 1
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.S8.QACC.LDBC.INCP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.U16.ACCX
qx 1, qy 1
—
ACCX 2
ACCX 2
EE.VMULAS.U16.ACCX.LD.IP
as 1, qx 1, qy 1
qu 2, as 1
ACCX 2
ACCX 2
EE.VMULAS.U16.ACCX.LD.IP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
ACCX 2
ACCX 2
EE.VMULAS.U16.ACCX.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1
ACCX 2
ACCX 2
Espressif Systems
71

<!-- page 72 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
EE.VMULAS.U16.ACCX.LD.XP.QUP
as 1, ad 1, qx 1,
qy 1, qs0 1, qs1
1
qu 2, as 1, qs0 1
SAR_BYTE
1,
ACCX 2
ACCX 2
EE.VMULAS.U16.QACC
qx 1, qy 1
—
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VMULAS.U16.QACC.LD.IP
as 1, qx 1, qy 1
qu 2, as 1
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VMULAS.U16.QACC.LD.IP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.U16.QACC.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VMULAS.U16.QACC.LD.XP.QUP
as 1, ad 1, qx 1,
qy 1, qs0 1, qs1
1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.U16.QACC.LDBC.INCP
as 1, qx 1, qy 1
qu 2, as 1
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VMULAS.U16.QACC.LDBC.INCP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.U8.ACCX
qx 1, qy 1
—
ACCX 2
ACCX 2
EE.VMULAS.U8.ACCX.LD.IP
as 1, qx 1, qy 1
qu 2, as 1
ACCX 2
ACCX 2
EE.VMULAS.U8.ACCX.LD.IP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
ACCX 2
ACCX 2
EE.VMULAS.U8.ACCX.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1
ACCX 2
ACCX 2
EE.VMULAS.U8.ACCX.LD.XP.QUP
as 1, ad 1, qx 1,
qy 1, qs0 1, qs1
1
qu 2, as 1, qs0 1
SAR_BYTE
1,
ACCX 2
ACCX 2
EE.VMULAS.U8.QACC
qx 1, qy 1
—
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VMULAS.U8.QACC.LD.IP
as 1, qx 1, qy 1
qu 2, as 1
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VMULAS.U8.QACC.LD.IP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.U8.QACC.LD.XP
as 1, ad 1, qx 1,
qy 1
qu 2, as 1
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VMULAS.U8.QACC.LD.XP.QUP
as 1, ad 1, qx 1,
qy 1, qs0 1, qs1
1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VMULAS.U8.QACC.LDBC.INCP
as 1, qx 1, qy 1
qu 2, as 1
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
Espressif Systems
72

<!-- page 73 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
EE.VMULAS.U8.QACC.LDBC.INCP.QUP
as 1, qx 1, qy 1,
qs0 1, qs1 1
qu 2, as 1, qs0 1
SAR_BYTE
1,
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VPRELU.S16
qx 1, qy 1, ay 1
qz 2
—
—
EE.VPRELU.S8
qx 1, qy 1, ay 1
qz 2
—
—
EE.VRELU.S16
qs 1, ax 1, ay 1
qs 2
—
—
EE.VRELU.S8
qs 1, ax 1, ay 1
qs 2
—
—
EE.VSL.32
qs 1
qa 1
SAR 1
—
EE.VSMULAS.S16.QACC
qx 1, qy 1
—
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VSMULAS.S16.QACC.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VSMULAS.S8.QACC
qx 1, qy 1
—
QACC_L
2,
QACC_H 2
QACC_L
2,
QACC_H 2
EE.VSMULAS.S8.QACC.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1
QACC_H
2,
QACC_L 2
QACC_H
2,
QACC_L 2
EE.VSR.32
qs 1
qa 1
SAR 1
—
EE.VST.128.IP
qv 1, as 1
as 1
—
—
EE.VST.128.XP
qv 1, as 1, ad 1
as 1
—
—
EE.VST.H.64.IP
qv 1, as 1
as 1
—
—
EE.VST.H.64.XP
qv 1, as 1, ad 1
as 1
—
—
EE.VST.L.64.IP
qv 1, as 1
as 1
—
—
EE.VST.L.64.XP
qv 1, as 1, ad 1
as 1
—
—
EE.VSUBS.S16
qx 1, qy 1
qa 1
—
—
EE.VSUBS.S16.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VSUBS.S16.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VSUBS.S32
qx 1, qy 1
qa 1
—
—
EE.VSUBS.S32.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VSUBS.S32.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VSUBS.S8
qx 1, qy 1
qa 1
—
—
EE.VSUBS.S8.LD.INCP
as 1, qx 1, qy 1
qu 2, as 1, qa 1
—
—
EE.VSUBS.S8.ST.INCP
qv 1, as 1, qx 1,
qy 1
as 1, qa 1
—
—
EE.VUNZIP.16
qs0 1, qs1 1
qs0 1, qs1 1
—
—
EE.VUNZIP.32
qs0 1, qs1 1
qs0 1, qs1 1
—
—
EE.VUNZIP.8
qs0 1, qs1 1
qs0 1, qs1 1
—
—
EE.VZIP.16
qs0 1, qs1 1
qs0 1, qs1 1
—
—
EE.VZIP.32
qs0 1, qs1 1
qs0 1, qs1 1
—
—
EE.VZIP.8
qs0 1, qs1 1
qs0 1, qs1 1
—
—
EE.WR_MASK_GPIO_OUT
as 1, ax 1
—
GPIO_OUT 1
GPIO_OUT 1
EE.XORQ
qx 1, qy 1
qa 1
—
—
EE.ZERO.ACCX
—
—
—
ACCX 1
Espressif Systems
73

<!-- page 74 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
EE.ZERO.Q
—
qa 1
—
—
EE.ZERO.QACC
—
—
—
QACC_L
1,
QACC_H 1
1.7.2
Hardware Resource Hazard
When multiple instructions call the same hardware resource at the same time, the processor allows only one
of the instructions to occupy the hardware resource, and the rest of them will be delayed. For example, there
are only eight 16-bit multipliers in the processor; instruction C requires eight of them in pipeline stage M, and
instruction D requires four of them in pipeline stage E. As shown in Figure 1.7-2, instruction C is issued in cycle
T+0, and instruction D is issued in cycle T+1, so four multipliers are applied to be occupied simultaneously in
cycle T+3; at this time, the processor will delay the issue of instruction D into the pipeline by one cycle to
avoid conflict with instruction C.
Figure 1.7-2. Hardware Resource Hazard
1.7.3
Control Hazard
Data and hardware resource hazards can be optimized by adjusting the code order, but the control hazard is
difficult to optimize. Program code usually has many conditional select statements that execute different code
depending on whether the condition is met or not. The compiler will process the above conditional
statements into branch and jump instructions: if the condition is satisfied, it will jump to the target address to
execute the corresponding code; if not, the subsequent instructions will be processed in order. When the
conditions are met, as shown in Figure 1.7-3, the processor will re-fetch the instruction from the new target
address. At this time, the instructions at the R and E stages on the pipeline will be removed, which means the
pipeline remains stagnant for 2 cycles.
Espressif Systems
74

<!-- page 75 -->
Chapter 1 Processor Instruction Extensions (PIE)
GoBack
Figure 1.7-3. Control Hazard
Espressif Systems
75
