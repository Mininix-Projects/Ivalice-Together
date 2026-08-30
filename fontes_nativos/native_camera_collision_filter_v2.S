.intel_syntax noprefix
.text
.global camera_collision_filter_v2

# Repeat the camera query when the first hit originated inside the party
# formation. Floors, ceilings, and walls outside that span remain native.
# coop_runtime.py fills the three marked addresses below.

camera_collision_filter_v2:
    sub rsp, 0x88
    mov qword ptr [rsp + 0x30], rcx
    mov qword ptr [rsp + 0x38], rdx
    mov qword ptr [rsp + 0x40], r8
    mov qword ptr [rsp + 0x48], r9

    mov rax, qword ptr [rsp + 0xB0]
    mov qword ptr [rsp + 0x20], rax
    mov rax, qword ptr [rsp + 0xB8]
    mov qword ptr [rsp + 0x28], rax

    mov rax, 0x5555555566666666
    call rax
    test eax, eax
    jz .return_result

    mov r11, 0x1111111122222222
    cmp dword ptr [r11 + 0x3C], 0
    je .return_result

    mov dword ptr [rsp + 0x50], 0
    mov dword ptr [rsp + 0x54], 0

.classify_hit:
    mov r10, 0x3333333344444444
    mov rdx, qword ptr [rsp + 0x38]

    cmp dword ptr [r10 + 0x44], 1
    jne .formation_gate
    mov r8, qword ptr [rsp + 0x40]
    movss xmm0, dword ptr [rdx]
    subss xmm0, dword ptr [r8]
    mulss xmm0, xmm0
    movss xmm1, dword ptr [rdx + 0x04]
    subss xmm1, dword ptr [r8 + 0x04]
    mulss xmm1, xmm1
    addss xmm0, xmm1
    movss xmm1, dword ptr [rdx + 0x08]
    subss xmm1, dword ptr [r8 + 0x08]
    mulss xmm1, xmm1
    addss xmm0, xmm1
    ucomiss xmm0, dword ptr [r10 + 0x48]
    jp .external_hit
    jbe .focus_internal_hit

.formation_gate:
    cmp dword ptr [r10 + 0x40], 1
    jne .return_result

    movss xmm0, dword ptr [rdx]
    subss xmm0, dword ptr [r10 + 0x20]
    movss xmm1, dword ptr [rdx + 0x08]
    subss xmm1, dword ptr [r10 + 0x28]

    movss xmm2, dword ptr [r10 + 0x2C]
    subss xmm2, dword ptr [r10 + 0x20]
    movss xmm3, dword ptr [r10 + 0x34]
    subss xmm3, dword ptr [r10 + 0x28]

    movaps xmm4, xmm2
    mulss xmm4, xmm4
    movaps xmm5, xmm3
    mulss xmm5, xmm5
    addss xmm4, xmm5
    ucomiss xmm4, dword ptr [rip + .epsilon]
    jp .external_hit
    ja .project_to_segment
    xorps xmm5, xmm5
    jmp .vertical_distance

.project_to_segment:

    movaps xmm5, xmm0
    mulss xmm5, xmm2
    movaps xmm4, xmm1
    mulss xmm4, xmm3
    addss xmm5, xmm4

    movaps xmm4, xmm2
    mulss xmm4, xmm4
    movaps xmm2, xmm3
    mulss xmm2, xmm2
    addss xmm4, xmm2
    divss xmm5, xmm4
    maxss xmm5, dword ptr [rip + .zero]
    minss xmm5, dword ptr [rip + .one]

    movss xmm2, dword ptr [r10 + 0x2C]
    subss xmm2, dword ptr [r10 + 0x20]
    mulss xmm2, xmm5
    subss xmm0, xmm2
    movss xmm3, dword ptr [r10 + 0x34]
    subss xmm3, dword ptr [r10 + 0x28]
    mulss xmm3, xmm5
    subss xmm1, xmm3

.vertical_distance:
    movss xmm4, dword ptr [r10 + 0x30]
    subss xmm4, dword ptr [r10 + 0x24]
    mulss xmm4, xmm5
    addss xmm4, dword ptr [r10 + 0x24]
    movss xmm5, dword ptr [rdx + 0x04]
    subss xmm5, xmm4
    mulss xmm5, xmm5
    ucomiss xmm5, dword ptr [r10 + 0x3C]
    jp .external_hit
    ja .external_hit

    mulss xmm0, xmm0
    mulss xmm1, xmm1
    addss xmm0, xmm1
    ucomiss xmm0, dword ptr [r10 + 0x38]
    jp .external_hit
    ja .external_hit

    jmp .internal_hit

.focus_internal_hit:
    inc dword ptr [r10 + 0x4C]

.internal_hit:
    inc dword ptr [r10 + 0x10]
    inc dword ptr [rsp + 0x54]
    cmp dword ptr [rsp + 0x54], 24
    ja .internal_exhausted

    mov r8, qword ptr [rsp + 0x40]
    mov r9, qword ptr [rsp + 0x48]
    movss xmm0, dword ptr [r9]
    subss xmm0, dword ptr [r8]
    movss xmm1, dword ptr [r9 + 0x08]
    subss xmm1, dword ptr [r8 + 0x08]
    movaps xmm2, xmm0
    mulss xmm2, xmm2
    movaps xmm3, xmm1
    mulss xmm3, xmm3
    addss xmm2, xmm3
    sqrtss xmm2, xmm2
    ucomiss xmm2, dword ptr [rip + .epsilon]
    jp .external_hit
    jbe .internal_exhausted

    mov rdx, qword ptr [rsp + 0x38]
    movss xmm3, dword ptr [rdx]
    subss xmm3, dword ptr [r8]
    mulss xmm3, xmm0
    movss xmm4, dword ptr [rdx + 0x08]
    subss xmm4, dword ptr [r8 + 0x08]
    mulss xmm4, xmm1
    addss xmm3, xmm4
    divss xmm3, xmm2
    addss xmm3, dword ptr [rip + .retry_step]

    movss xmm4, dword ptr [rsp + 0x50]
    addss xmm4, dword ptr [rip + .retry_step]
    maxss xmm3, xmm4
    movss dword ptr [rsp + 0x50], xmm3

    divss xmm3, xmm2
    ucomiss xmm3, dword ptr [rip + .maximum_ratio]
    jp .external_hit
    jae .internal_exhausted

    movss xmm4, dword ptr [r9]
    subss xmm4, dword ptr [r8]
    mulss xmm4, xmm3
    addss xmm4, dword ptr [r8]
    movss dword ptr [rsp + 0x60], xmm4

    movss xmm4, dword ptr [r9 + 0x04]
    subss xmm4, dword ptr [r8 + 0x04]
    mulss xmm4, xmm3
    addss xmm4, dword ptr [r8 + 0x04]
    movss dword ptr [rsp + 0x64], xmm4

    movss xmm4, dword ptr [r9 + 0x08]
    subss xmm4, dword ptr [r8 + 0x08]
    mulss xmm4, xmm3
    addss xmm4, dword ptr [r8 + 0x08]
    movss dword ptr [rsp + 0x68], xmm4

    movss xmm4, dword ptr [r8 + 0x0C]
    movss dword ptr [rsp + 0x6C], xmm4

    mov rcx, qword ptr [rsp + 0x30]
    mov rdx, qword ptr [rsp + 0x38]
    lea r8, [rsp + 0x60]
    mov r9, qword ptr [rsp + 0x48]
    mov r10, 0x3333333344444444
    inc dword ptr [r10 + 0x14]
    mov rax, 0x5555555566666666
    call rax
    test eax, eax
    jnz .classify_hit

    jmp .return_result

.external_hit:
    mov r10, 0x3333333344444444
    inc dword ptr [r10 + 0x1C]
    mov eax, 1
    jmp .return_result

.internal_exhausted:
    mov r10, 0x3333333344444444
    inc dword ptr [r10 + 0x18]
    xor eax, eax

.return_result:
    add rsp, 0x88
    ret

.align 4
.zero:
    .long 0x00000000
.one:
    .long 0x3F800000
.epsilon:
    .long 0x358637BD
.retry_step:
    .long 0x3F400000
.maximum_ratio:
    .long 0x3F7AE148
