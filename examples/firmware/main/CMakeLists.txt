idf_component_register(
    SRCS
        "main.c"
        "ex01_encoding.S"
        "ex02_matmul.S"
        "ex03_fir.S"
        "ex04_qr.S"
        "ex05_saturation.S"
        "ex06_fft.S"
        "ex07_transform3d.S"
        "ex08_media.S"
        "ex09_qacc.S"
        "ex10_motion.S"
        "ex11_block8x8.S"
        "ex12_physics.S"
    INCLUDE_DIRS ".")

# The examples time hand-written assembly against an equivalent C loop. ESP-IDF's default for application
# code is -Og, which would flatter the assembly for the wrong reason, so the C reference is built at -O2:
# the comparison is meant to be instruction-set against instruction-set, not against a debug build.
target_compile_options(${COMPONENT_LIB} PRIVATE -O2)
