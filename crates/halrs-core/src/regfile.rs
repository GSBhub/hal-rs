//! Data-driven peripheral model built from an SVD/RDL-derived [`Peripheral`].
//!
//! Registers keep their reset value and per-field access semantics. Offsets with
//! no described register behave like plain RAM so firmware that writes and reads
//! back undocumented registers still works. Status bits tagged with a [`Hint`]
//! read as "ready", and a poll detector breaks loops the hints missed.

use std::collections::HashMap;

use crate::config::{Access, Hint, Peripheral, Register};

/// Consecutive unchanged reads of one register before the poll detector kicks in.
pub const POLL_THRESHOLD: u32 = 64;

#[derive(Debug)]
struct RegState {
    desc: Register,
    value: u64,
    /// Bits forced to 1 / to 0 on read (from hints).
    force_set: u64,
    force_clear: u64,
    write_only: u64,
    read_only: u64,
    w1c: u64,
    read_clear: u64,
    /// (dest mask, dest lsb, src lsb) for mirrored status fields.
    mirrors: Vec<(u64, u32, u32)>,
}

impl RegState {
    fn new(desc: Register) -> Self {
        let mut s = RegState {
            value: desc.reset,
            force_set: 0,
            force_clear: 0,
            write_only: 0,
            read_only: 0,
            w1c: 0,
            read_clear: 0,
            mirrors: Vec::new(),
            desc,
        };
        for f in &s.desc.fields {
            let m = f.mask();
            match f.access {
                Access::ReadWrite => {}
                Access::ReadOnly => s.read_only |= m,
                Access::WriteOnly => s.write_only |= m,
                Access::W1c => s.w1c |= m,
                Access::ReadClear => s.read_clear |= m,
            }
            match f.hint {
                Some(Hint::PollSet) => s.force_set |= m,
                Some(Hint::PollClear) => s.force_clear |= m,
                Some(Hint::Mirror(src)) => s.mirrors.push((m, f.lsb, src)),
                None => {}
            }
        }
        s
    }

    fn bytes(&self) -> u64 {
        u64::from(self.desc.size.div_ceil(8))
    }

    fn read(&mut self) -> u64 {
        let mut v = self.value;
        for &(mask, dst, src) in &self.mirrors {
            v = (v & !mask) | (((self.value >> src) << dst) & mask);
        }
        let v = ((v | self.force_set) & !self.force_clear) & !self.write_only;
        self.value &= !self.read_clear;
        v
    }

    /// `lanes`: the bits the access actually wrote (narrow writes touch fewer).
    fn write(&mut self, new: u64, lanes: u64) {
        let keep = self.read_only | !lanes;
        let mut v = (self.value & keep) | (new & !keep & !self.w1c);
        // W1C: writing 1 clears, writing 0 leaves the bit alone.
        v |= self.value & self.w1c & (!new | !lanes);
        self.value = v;
    }
}

#[derive(Debug, Default)]
struct PollDetector {
    last: Option<(u64, u64)>,
    count: u32,
    tripped: bool,
}

/// Outcome of a read, so the caller can log what the model did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadNote {
    Normal,
    /// The poll detector flipped the value to break a wait loop.
    PollBroken,
}

#[derive(Debug)]
pub struct PeriphModel {
    pub name: String,
    pub size: u64,
    regs: Vec<RegState>,
    /// Plain storage for offsets that no register describes (word aligned).
    scratch: HashMap<u64, u32>,
    poll: PollDetector,
    flip: bool,
}

impl PeriphModel {
    pub fn new(p: &Peripheral) -> Self {
        PeriphModel {
            name: p.name.clone(),
            size: p.size,
            regs: p.registers.iter().cloned().map(RegState::new).collect(),
            scratch: HashMap::new(),
            poll: PollDetector::default(),
            flip: false,
        }
    }

    fn find(&self, offset: u64) -> Option<usize> {
        self.regs
            .iter()
            .position(|r| offset >= r.desc.offset && offset < r.desc.offset + r.bytes())
    }

    pub fn read(&mut self, offset: u64, size: u32) -> (u64, ReadNote) {
        let mask = size_mask(size);
        let Some(i) = self.find(offset) else {
            let word = self.scratch.get(&(offset & !3)).copied().unwrap_or(0);
            return ((u64::from(word) >> ((offset & 3) * 8)) & mask, ReadNote::Normal);
        };
        let shift = (offset - self.regs[i].desc.offset) * 8;
        let full = self.regs[i].read();

        let reg_off = self.regs[i].desc.offset;
        let note = self.observe_read(reg_off, full);
        let full = if note == ReadNote::PollBroken {
            self.flip = !self.flip;
            if self.flip { !full } else { full }
        } else {
            full
        };
        ((full >> shift) & mask, note)
    }

    pub fn write(&mut self, offset: u64, value: u64, size: u32) {
        self.poll = PollDetector::default();
        let mask = size_mask(size);
        let Some(i) = self.find(offset) else {
            let word = self.scratch.entry(offset & !3).or_insert(0);
            let shift = (offset & 3) * 8;
            let m = (mask << shift) as u32;
            *word = (*word & !m) | (((value & mask) << shift) as u32 & m);
            return;
        };
        let r = &mut self.regs[i];
        let shift = (offset - r.desc.offset) * 8;
        r.write((value & mask) << shift, mask << shift);
    }

    /// Track consecutive identical reads of the same register.
    fn observe_read(&mut self, reg_off: u64, value: u64) -> ReadNote {
        let p = &mut self.poll;
        if p.last == Some((reg_off, value)) || (p.tripped && p.last.map(|l| l.0) == Some(reg_off)) {
            p.count += 1;
        } else {
            *p = PollDetector { last: Some((reg_off, value)), count: 1, tripped: false };
        }
        if p.count >= POLL_THRESHOLD {
            p.tripped = true;
            return ReadNote::PollBroken;
        }
        ReadNote::Normal
    }

    pub fn reg_value(&self, name: &str) -> Option<u64> {
        self.regs.iter().find(|r| r.desc.name == name).map(|r| r.value)
    }
}

fn size_mask(size: u32) -> u64 {
    if size >= 8 { u64::MAX } else { (1u64 << (size * 8)) - 1 }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Field;

    fn field(name: &str, lsb: u32, width: u32, access: Access, hint: Option<Hint>) -> Field {
        Field { name: name.into(), lsb, width, access, hint }
    }

    fn uart() -> PeriphModel {
        PeriphModel::new(&Peripheral {
            name: "USART2".into(),
            base: 0x4000_4400,
            size: 0x400,
            irqs: vec![38],
            model: Default::default(),
            registers: vec![
                Register {
                    name: "SR".into(),
                    offset: 0,
                    size: 32,
                    reset: 0x00C0,
                    fields: vec![
                        field("TXE", 7, 1, Access::ReadOnly, Some(Hint::PollSet)),
                        field("RXNE", 5, 1, Access::W1c, None),
                        field("BSY", 9, 1, Access::ReadOnly, Some(Hint::PollClear)),
                    ],
                },
                Register { name: "DR".into(), offset: 4, size: 32, reset: 0, fields: vec![] },
                Register {
                    name: "KEY".into(),
                    offset: 8,
                    size: 32,
                    reset: 0,
                    fields: vec![field("KEY", 0, 16, Access::WriteOnly, None)],
                },
            ],
        })
    }

    #[test]
    fn reset_and_hints() {
        let mut m = uart();
        let (sr, _) = m.read(0, 4);
        assert_eq!(sr & (1 << 7), 1 << 7, "TXE forced set");
        assert_eq!(sr & (1 << 9), 0, "BSY forced clear");
    }

    #[test]
    fn read_only_and_w1c() {
        let mut m = uart();
        m.write(0, 1 << 5, 4); // RXNE is W1C: writing 1 to an already-0 bit keeps it 0
        assert_eq!(m.reg_value("SR").unwrap() & (1 << 5), 0);
        m.regs[0].value |= 1 << 5;
        m.write(0, 0, 4); // writing 0 leaves W1C bit alone
        assert_ne!(m.reg_value("SR").unwrap() & (1 << 5), 0);
        m.write(0, 1 << 5, 4);
        assert_eq!(m.reg_value("SR").unwrap() & (1 << 5), 0);
        m.write(0, 0, 4); // TXE is RO: reset value kept
        assert_ne!(m.reg_value("SR").unwrap() & (1 << 7), 0);
    }

    #[test]
    fn narrow_write_leaves_other_lanes_w1c() {
        let mut m = uart();
        m.regs[0].value |= 1 << 5;
        m.write(1, 0, 1); // byte 1 only: RXNE (byte 0) must survive
        assert_ne!(m.reg_value("SR").unwrap() & (1 << 5), 0);
        m.write(0, 1 << 5, 1);
        assert_eq!(m.reg_value("SR").unwrap() & (1 << 5), 0);
    }

    #[test]
    fn mirrored_status_field() {
        // RCC_CFGR: SW[1:0] (rw), SWS[3:2] (ro) reports SW.
        let mut m = PeriphModel::new(&Peripheral {
            name: "RCC".into(),
            base: 0x4002_3800,
            size: 0x400,
            irqs: vec![],
            model: Default::default(),
            registers: vec![Register {
                name: "CFGR".into(),
                offset: 8,
                size: 32,
                reset: 0,
                fields: vec![
                    field("SW", 0, 2, Access::ReadWrite, None),
                    field("SWS", 2, 2, Access::ReadOnly, Some(Hint::Mirror(0))),
                ],
            }],
        });
        m.write(8, 0b10, 4);
        assert_eq!(m.read(8, 4).0, 0b1010);
    }

    #[test]
    fn write_only_reads_zero() {
        let mut m = uart();
        m.write(8, 0xCDEF, 4);
        assert_eq!(m.read(8, 4).0, 0);
    }

    #[test]
    fn sub_word_access() {
        let mut m = uart();
        m.write(5, 0xAB, 1);
        assert_eq!(m.read(4, 4).0, 0xAB00);
        assert_eq!(m.read(5, 1).0, 0xAB);
    }

    #[test]
    fn undescribed_offsets_are_ram() {
        let mut m = uart();
        m.write(0x100, 0xDEAD_BEEF, 4);
        assert_eq!(m.read(0x100, 4).0, 0xDEAD_BEEF);
        assert_eq!(m.read(0x102, 2).0, 0xDEAD);
    }

    #[test]
    fn poll_detector_breaks_loops() {
        let mut m = uart();
        let first = m.read(4, 4).0;
        let mut broke = false;
        for _ in 0..POLL_THRESHOLD {
            let (v, note) = m.read(4, 4);
            if note == ReadNote::PollBroken {
                assert_ne!(v, first);
                broke = true;
                break;
            }
        }
        assert!(broke);
        // A write resets the detector.
        m.write(4, 0, 4);
        assert_eq!(m.read(4, 4).1, ReadNote::Normal);
    }
}
