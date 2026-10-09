//! Thumb-2 (ARMv7-M) bits needed for pass-through intercepts.
//!
//! A trampoline intercept rewrites the function entry to `b.w T` where
//!
//! ```text
//! T+0  cmp.w r12, #0     <- hooked; the handler sets r12 = 1 (intercepted) or 0
//! T+4  beq.n T+8         <- r12 == 0: fall through to the original function
//! T+6  bx lr             <- r12 != 0: return (r0 already set by the handler)
//! T+8  <relocated entry instructions, >= 4 bytes>
//!      b.w entry + n
//! ```
//!
//! r12 (IP) may be clobbered at a call boundary under AAPCS (linker veneers do),
//! and is stacked by hardware on exception entry, so using it as the flag is safe.
//! Relocation is deliberately conservative: only an allowlist of
//! position-independent prologue instructions is copied.

/// Bytes reserved per trampoline (max 8 + 6 relocated + 4).
pub const SLOT: u64 = 20;

fn hw(code: &[u8], off: usize) -> Option<u16> {
    Some(u16::from_le_bytes([*code.get(off)?, *code.get(off + 1)?]))
}

/// Length of the instruction whose first halfword is `h1`.
pub fn insn_len(h1: u16) -> usize {
    match h1 >> 11 {
        0b11101 | 0b11110 | 0b11111 => 4,
        _ => 2,
    }
}

/// Is this instruction safe to execute from a different address?
fn relocatable(h1: u16, h2: Option<u16>) -> bool {
    let Some(h2) = h2 else {
        return match h1 >> 8 {
            // push (without/with lr); pop would be a return when it includes pc.
            0xB4 | 0xB5 => true,
            // add/sub sp, sp, #imm
            0xB0 => true,
            // nop / hints (not IT, which conditions the next instructions).
            0xBF => h1 & 0x000F == 0,
            _ => match h1 >> 11 {
                // shifts, add/sub, mov/cmp/add/sub imm8 (low registers)
                0b00000..=0b00111 => true,
                // str/ldr reg-offset and imm-offset, sp-relative, add rd, sp
                0b01010..=0b01011 | 0b01100..=0b10011 | 0b10101 => true,
                // data processing (low registers)
                _ if h1 >> 10 == 0b010000 => true,
                // hi-register mov/add/cmp, as long as neither operand is pc
                _ if h1 >> 10 == 0b010001 && (h1 >> 8) & 0b11 != 0b11 => {
                    let rm = (h1 >> 3) & 0xF;
                    let rd = (h1 & 0x7) | ((h1 >> 4) & 0x8);
                    rm != 15 && rd != 15
                }
                _ => false,
            },
        };
    };
    let rn = h1 & 0xF;
    match h1 >> 11 {
        // Data processing (immediate); h2 bit 15 set means branch / misc control.
        0b11110 => h2 & 0x8000 == 0 && rn != 15,
        // Load/store single (Rn = pc is a literal load).
        0b11111 => (h1 >> 9) & 0b11 == 0b00 && rn != 15,
        0b11101 => match (h1 >> 9) & 0b11 {
            // Load/store multiple / dual / exclusive / table branch.
            0b00 => {
                let is_load = h1 & (1 << 4) != 0;
                let table_branch = h1 & 0xFFF0 == 0xE8D0 && h2 & 0xFFE0 == 0xF000;
                rn != 15 && !table_branch && !(is_load && h2 & 0x8000 != 0) // ldm {pc} returns
            }
            // Data processing (shifted register); Rd/Rn = pc are special forms.
            0b01 => rn != 15 && (h2 >> 8) & 0xF != 15,
            _ => h1 == 0xED2D, // vpush
        },
        _ => false,
    }
}

/// Bytes of whole instructions at `code[0..]` covering at least 4 bytes, if all
/// of them can run from another address.
pub fn relocatable_prefix(code: &[u8]) -> Option<usize> {
    let mut off = 0;
    while off < 4 {
        let h1 = hw(code, off)?;
        let len = insn_len(h1);
        let h2 = if len == 4 { Some(hw(code, off + 2)?) } else { None };
        if !relocatable(h1, h2) {
            return None;
        }
        off += len;
    }
    Some(off)
}

/// `b.w target` placed at `from` (encoding T4).
pub fn b_w(from: u64, target: u64) -> Option<[u8; 4]> {
    let off = target as i64 - (from as i64 + 4);
    if off & 1 != 0 || !(-(1 << 24)..(1 << 24)).contains(&off) {
        return None;
    }
    let imm = (off >> 1) as u32;
    let s = (imm >> 23) & 1;
    let i1 = (imm >> 22) & 1;
    let i2 = (imm >> 21) & 1;
    let j1 = (!(i1 ^ s)) & 1;
    let j2 = (!(i2 ^ s)) & 1;
    let h1 = 0xF000 | (s << 10) | ((imm >> 11) & 0x3FF);
    let h2 = 0x9000 | (j1 << 13) | (j2 << 11) | (imm & 0x7FF);
    let mut out = [0u8; 4];
    out[..2].copy_from_slice(&(h1 as u16).to_le_bytes());
    out[2..].copy_from_slice(&(h2 as u16).to_le_bytes());
    Some(out)
}

/// Build the trampoline for a function at `entry` whose first bytes are `code`,
/// to live at `tramp`. Returns `(entry_patch, trampoline_bytes)`, or `None` if
/// the entry instructions can't be relocated or the branch is out of range.
pub fn trampoline(entry: u64, code: &[u8], tramp: u64) -> Option<([u8; 4], Vec<u8>)> {
    let n = relocatable_prefix(code)?;
    let mut t = Vec::with_capacity(SLOT as usize);
    t.extend_from_slice(&[0xBC, 0xF1, 0x00, 0x0F]); // cmp.w r12, #0
    t.extend_from_slice(&[0x00, 0xD0]); // beq.n T+8
    t.extend_from_slice(&[0x70, 0x47]); // bx lr
    t.extend_from_slice(&code[..n]);
    let back = b_w(tramp + t.len() as u64, entry + n as u64)?;
    t.extend_from_slice(&back);
    Some((b_w(entry, tramp)?, t))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    #[test]
    fn b_w_matches_keystone() {
        assert_eq!(hex(&b_w(0x0800_125c, 0x081f_fe00).unwrap()), "fef1d0bd");
        assert_eq!(hex(&b_w(0x081f_fe10, 0x0800_1260).unwrap()), "01f626ba");
        assert!(b_w(0x0800_0000, 0x0A00_0000).is_none(), "out of +-16MiB range");
    }

    #[test]
    fn prologues() {
        // From the HALucinator STM32 example firmware.
        assert_eq!(relocatable_prefix(&[0x80, 0xb5, 0x82, 0xb0]), Some(4)); // push {r7,lr}; sub sp,#8
        assert_eq!(relocatable_prefix(&[0x80, 0xb4, 0x00, 0xaf]), Some(4)); // push {r7}; add r7,sp,#0
        assert_eq!(relocatable_prefix(&[0x2d, 0xe9, 0xf0, 0x41]), Some(4)); // push.w {r4-r8,lr}
        assert_eq!(relocatable_prefix(&[0x03, 0x4b, 0x00, 0xbf]), None); // ldr r3,[pc,#12]
        assert_eq!(relocatable_prefix(&[0x70, 0x47, 0x00, 0xbf]), None); // bx lr
        assert_eq!(relocatable_prefix(&[0x00, 0xb9, 0x00, 0xbf]), None); // cbnz
        assert_eq!(relocatable_prefix(&[0xdf, 0xf8, 0x10, 0x30]), None); // ldr.w r3,[pc,#16]
        assert_eq!(relocatable_prefix(&[0xbd, 0xe8, 0xf0, 0x81]), None); // pop.w {..., pc}
        assert_eq!(relocatable_prefix(&[0xd3, 0xe8, 0x02, 0xf0]), None); // tbb [r3, r2]
        assert_eq!(relocatable_prefix(&[0x80, 0xb5]), None, "truncated");
    }

    #[test]
    fn trampoline_layout() {
        let (patch, t) = trampoline(0x0800_125c, &[0x80, 0xb5, 0x82, 0xb0], 0x081f_fe00).unwrap();
        assert_eq!(hex(&patch), "fef1d0bd");
        assert_eq!(hex(&t[..8]), "bcf1000f00d07047");
        assert_eq!(hex(&t[8..12]), "80b582b0");
        // b.w back from T+12 to entry+4 (keystone: b.w 0x08001260 @ 0x081ffe0c)
        assert_eq!(&t[12..], &b_w(0x081f_fe0c, 0x0800_1260).unwrap());
        assert!(t.len() as u64 <= SLOT);
    }
}
