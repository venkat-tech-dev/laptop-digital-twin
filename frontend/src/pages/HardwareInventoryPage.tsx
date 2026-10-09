import { useState } from 'react'

import photo from '../assets/figma/inventory/laptop-photo.jpg'
import { useCoverage } from '../app/derive'
import { exportJson } from '../app/exportData'
import { bytesGB, cleanCpuName, cleanGpuName } from '../app/liveData'
import { DeviceEventsPanel, PosturePanel } from '../app/EndpointPanels'
import { useModelTwin } from '../app/modelTwin'
import { navigate } from '../app/routes'
import { api } from '../services/api'
import { canAdmin, useSession } from '../stores/sessionStore'
import { Twin3D } from '../app/Twin3D'
import { useInventory, arr, obj, str } from '../hooks/useInventory'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { useTwinStore } from '../stores/twinStore'
import type { TwinComponent } from '../types/telemetry'
import { formatDuration } from '../utils/format'
import { firstOfType, num, ofType, readingOf } from '../utils/twin'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, DataTable, Panel, PanelHeading, Spec } from '../ui/primitives'

interface HwRow {
  component: string
  id: string
  config: string
  state: string
  warn: boolean
}

const stateOf = (c: TwinComponent | undefined): { state: string; warn: boolean } => {
  if (!c) return { state: 'Not detected', warn: true }
  if (c.health.status === 'critical') return { state: 'Critical', warn: true }
  if (c.health.status === 'warning') return { state: 'Advisory', warn: true }
  if (c.availability === 'no_telemetry') return { state: 'Passive', warn: false }
  return { state: c.availability === 'partial' ? 'Nominal · partial sensors' : 'Nominal', warn: false }
}

export function HardwareInventoryPage() {
  const { inventory, discoveredAt } = useInventory()
  const components = useTwinStore((s) => s.components)
  const device = useTwinStore((s) => s.device)
  const { status } = useLiveStatus()
  const coverage = useCoverage()
  const inv = obj(inventory)
  const cpu = obj(inv.cpu)
  const mem = obj(inv.memory)
  const mods = arr(mem.modules)
  const bios = obj(inv.bios)
  const board = obj(inv.motherboard)
  const os = obj(inv.os)
  const battery = obj(inv.battery)
  const panel = obj(obj(inv.display).panel_size)
  const gpus = arr(inv.gpu)
  const disks = arr(inv.storage)
  const nics = arr(inv.network)
  const uptime = num(readingOf(components.os, 'system.uptime_s'))
  const fan = readingOf(components.fan, 'fan.speed_rpm')
  const twin = useModelTwin()
  const geometry = twin.geometry
  const me = useSession((st) => st.me)
  const admin = canAdmin(me)
  const [showPhoto, setShowPhoto] = useState(true)
  const [uploadMsg, setUploadMsg] = useState<string | null>(null)
  const photoUrl = geometry?.photo_url ?? null
  const secureBoot = readingOf(components.motherboard, 'security.secure_boot_enabled')
  const tpmPresent = readingOf(components.motherboard, 'security.tpm_present')
  const tpmVersion = readingOf(components.motherboard, 'security.tpm_version')
  const upload = async (kind: 'mesh' | 'photo', file: File | undefined) => {
    if (!file) return
    setUploadMsg(`Uploading ${file.name}…`)
    try {
      const attribution = kind === 'photo' ? 'Own photo' : (prompt('Attribution / licence of this model (optional):') ?? undefined)
      await api.uploadAsset(kind, file, { attribution: attribution || undefined, exact: false })
      useTwinStore.getState().setDevice(await api.device())
      setShowPhoto(kind === 'photo')
      setUploadMsg(kind === 'photo' ? 'Photo saved for this model.' : '3D model saved: shown as MATCHED MODEL.')
    } catch (e) {
      setUploadMsg(`Upload failed: ${e instanceof Error ? e.message : String(e)}`)
    }
    setTimeout(() => setUploadMsg(null), 5000)
  }
  const profile = twin.geometry?.profile ?? null
  const diag = panel.diagonal_in ? `${Number(panel.diagonal_in).toFixed(0)}-inch` : null

  const rows: HwRow[] = [
    { component: 'CPU', id: cleanCpuName(str(cpu.model)), config: `${str(cpu.cores) ?? '—'} cores / ${str(cpu.threads) ?? '—'} threads / ${cpu.base_clock_mhz ? (Number(cpu.base_clock_mhz) / 1000).toFixed(1) : '—'} GHz base`, ...stateOf(components.cpu) },
    ...gpus.map((g) => ({ component: 'GPU', id: cleanGpuName(str(g.name)), config: `${bytesGB(Number(g.dedicated_vram_bytes) || null)} dedicated / driver ${str(g.driver_version) ?? '—'}`, ...stateOf(firstOfType(components, 'gpu')) })),
    { component: 'Memory', id: `${str(mods[0]?.manufacturer) ?? '—'} ${str(mods[0]?.type) ?? ''}`.trim(), config: `${bytesGB(Number(mem.total_bytes) || null)} / ${str(mods[0]?.configured_speed_mts) ?? '—'} MT/s / ${mods.length} × ${str(mods[0]?.form_factor) ?? 'module'}`, ...stateOf(components.memory) },
    ...disks.map((d) => ({ component: 'Storage', id: str(d.model) ?? '—', config: `${bytesGB(Number(d.size_bytes) || null)} / ${str(d.bus_type) ?? str(d.interface) ?? '—'} / ${str(d.media_type) ?? '—'}`, ...stateOf(firstOfType(components, 'disk')) })),
    { component: 'Motherboard', id: `${str(board.manufacturer) ?? ''} ${str(board.product) ?? ''}`.trim() || '—', config: `revision ${str(board.version) ?? '—'}`, ...stateOf(components.motherboard) },
    { component: 'Battery', id: `${str(battery.manufacturer) ?? ''} ${str(battery.chemistry) ?? ''}`.trim() || (inventory && !inv.battery ? 'No battery' : '—'), config: `${str(battery.design_capacity_wh) ?? '—'} Wh design / ${str(battery.full_charge_capacity_wh) ?? '—'} Wh full`, ...stateOf(components.battery) },
    { component: 'Cooling', id: 'Fan + heat pipe', config: fan?.availability === 'available' ? `Fan ${Number(fan.value).toFixed(0)} RPM` : 'Fan RPM not exposed / ACPI thermal zones', ...stateOf(components.thermal_sensors) },
    { component: 'Display', id: diag ? `${diag} panel` : 'Internal panel', config: `${gpus[0] ? str(gpus[0].resolution) ?? '—' : '—'} / ${gpus[0] ? str(gpus[0].refresh_rate_hz) ?? '—' : '—'} Hz`, ...stateOf(components.display) },
    ...(profile ? [
      { component: 'Chassis', id: `${profile.name} · ${profile.colour.name}`, config: `${profile.chassis_mm.width} × ${profile.chassis_mm.depth} × ${profile.chassis_mm.height} mm / ${profile.internals.sodimm_slots} SODIMM slots (${mods.length} used) / ${profile.internals.ssd}`, state: 'Model profile', warn: false },
      { component: 'Ports · left', id: profile.ports.left.map((x) => x.label.split(' (')[0]).join(', '), config: profile.ports.left.map((x) => x.label).filter((l) => l.includes('(')).map((l) => l.slice(l.indexOf('(') + 1, -1)).join(' / ') || '—', state: 'Model profile', warn: false },
      { component: 'Ports · right', id: profile.ports.right.map((x) => x.label.split(' (')[0]).join(', '), config: profile.ports.right.map((x) => x.label).filter((l) => l.includes('(')).map((l) => l.slice(l.indexOf('(') + 1, -1)).join(' / ') || '—', state: 'Model profile', warn: false },
    ] : []),
    ...nics.map((n) => ({ component: 'Network', id: str(n.name) ?? '—', config: `${str(n.interface) ?? '—'} / ${str(n.type) ?? '—'}`, ...stateOf(ofType(components, 'network_adapter').find((c) => c.properties.interface === n.interface)) })),
  ]

  return (
    <>
      <PageHeading title="Hardware Inventory"
        subtitle={`Detected configuration / Discovery snapshot / ${discoveredAt ? new Date(discoveredAt).toLocaleString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC' : '—'}`}
        action={<Button icon="arrowRight" onClick={() => exportJson('hardware-inventory', inventory)}>Export inventory</Button>} />

      <div className="split split--rev">
        <div className="viewport viewport--photo">
          {showPhoto && photoUrl ? (
            <div className="viewport__image"><img src={photoUrl} alt={`Photo of this ${str(inv.model) ?? 'laptop'}`} /></div>
          ) : twin.modelSpecific ? (
            <Twin3D view={{ camera: 'photo', xray: false, explode: 0, thermal: false }} interactive
              fallback={<img className="viewport__fallback" src={photo} alt="" />} />
          ) : (
            <div className="viewport__image"><img src={photo} alt="Illustrative render of a laptop" /></div>
          )}
          <div className="viewport__asset-tools">
            {photoUrl ? (
              <Button active={showPhoto} onClick={() => setShowPhoto(!showPhoto)}>{showPhoto ? '3D model' : 'Photo'}</Button>
            ) : null}
            {admin ? (
              <>
                <label className="btn" title="Upload a photo of this laptop (JPEG / PNG / WebP, max 12 MB)">
                  Upload photo
                  <input type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={(e) => upload('photo', e.target.files?.[0])} />
                </label>
                <label className="btn" title="Upload a binary glTF (.glb) model of this laptop (max 60 MB). Only use models you have the right to use.">
                  Upload 3D model
                  <input type="file" accept=".glb,model/gltf-binary" hidden onChange={(e) => upload('mesh', e.target.files?.[0])} />
                </label>
              </>
            ) : null}
          </div>
          <div className="viewport__status viewport__status--left">
            <Chip tone={status === 'OFFLINE' ? 'critical' : 'accent'}>{status === 'OFFLINE' ? 'OFFLINE' : 'LIVE DEVICE'}</Chip>
          </div>
          <p className="viewport__caption viewport__caption--photo">{`${str(inv.manufacturer) ?? '—'} / ${str(inv.model) ?? '—'}${diag ? ` / ${diag.toUpperCase()}` : ''}`.toUpperCase()} · {showPhoto && photoUrl ? `PHOTO${geometry?.photo_attribution ? ` · ${geometry.photo_attribution.toUpperCase()}` : ''}` : twin.modelSpecific ? `${twin.label} · DRAG TO ROTATE` : 'ILLUSTRATIVE RENDER'}</p>
          {uploadMsg ? <p className="viewport__upload-msg">{uploadMsg}</p> : null}
        </div>
        <Panel style={{ flex: '868 0 0' }}>
          <PanelHeading eyebrow={`${(str(inv.pc_system_type) ?? 'DEVICE').toUpperCase()} / DEVICE DOSSIER`} title={`${str(inv.manufacturer) ?? ''} ${str(inv.model) ?? ''}`.trim() || 'No device'}
            right={<Chip tone={inventory ? 'accent' : 'muted'}>{inventory ? 'DISCOVERY COMPLETE' : 'WAITING FOR AGENT'}</Chip>} />
          <div className="spec-columns">
            <div>
              <Spec label="Manufacturer" value={str(inv.manufacturer) ?? '—'} />
              <Spec label="Model" value={str(inv.model) ?? '—'} />
              <Spec label="Device ID" value={device?.device_id ?? '—'} />
              <Spec label="Model number" value={str(inv.model_number) ?? '—'} />
            </div>
            <div>
              <Spec label="BIOS" value={`${str(bios.version) ?? '—'}`} />
              <Spec label="Chassis" value={`${diag ?? '—'} / ${(str(inv.pc_system_type) ?? '—').toLowerCase()}`} />
              <Spec label="TPM" value={tpmPresent?.availability === 'available' ? (tpmPresent.value ? `Present · TPM ${String(tpmVersion?.value ?? '?')}` : 'Not present') : 'Unavailable'}
                tone={tpmPresent?.availability === 'available' ? (tpmPresent.value ? 'accent' : 'amber') : 'na'} title={tpmPresent?.availability === 'available' ? tpmPresent.source : tpmPresent?.reason ?? 'Waiting for the agent'} />
              <Spec label="Last discovery" value={discoveredAt ? new Date(discoveredAt).toISOString().slice(11, 19) + ' UTC' : '—'} />
            </div>
          </div>
          <p className="note--muted note" style={{ fontSize: 11 }}>SMBIOS + Windows WMI + DXGI / detected on this device / serial numbers withheld by default (privacy)</p>
        </Panel>
      </div>

      <div className="split">
        <Panel style={{ flex: '868 0 0' }}>
          <PanelHeading title="Detected hardware" right={<p className="panel-meta">{String(rows.length).padStart(2, '0')} DEVICE GROUPS</p>} />
          <DataTable rowKey={(r) => `${r.component}-${r.id}`} rows={rows}
            columns={[
              { key: 'c', header: 'COMPONENT', width: 116, kind: 'primary', render: (r) => r.component },
              { key: 'i', header: 'IDENTIFICATION', width: 250, render: (r) => r.id },
              { key: 'g', header: 'CONFIGURATION', width: 300, render: (r) => r.config },
              { key: 's', header: 'STATE', grow: true, render: (r) => r.state, cellKind: (r) => (r.warn ? 'amber' : 'accent') },
            ]} />
          <p className="eyebrow">DEVICE / {device?.device_id ?? '—'} / DISCOVERED {discoveredAt ? new Date(discoveredAt).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }).toUpperCase() : '—'}</p>
        </Panel>
        <div className="side-stack">
          <Panel>
            <PanelHeading title="Operating system" />
            <div>
              <Spec label="Edition" value={str(os.name)?.replace(/^Microsoft\s+/, '') ?? '—'} />
              <Spec label="Version / build" value={`${str(os.version) ?? '—'} / ${str(os.build) ?? '—'}`} />
              <Spec label="Architecture" value={str(os.architecture) ?? '—'} />
              <Spec label="Uptime" value={uptime !== null ? formatDuration(uptime) : '—'} />
              <Spec label="Secure Boot" value={secureBoot?.availability === 'available' ? (secureBoot.value ? 'Enabled' : 'Disabled') : 'Unavailable'}
                tone={secureBoot?.availability === 'available' ? (secureBoot.value ? 'accent' : 'amber') : 'na'}
                title={secureBoot?.availability === 'available' ? `${secureBoot.source}${secureBoot.value ? '' : ' · UEFI Secure Boot is off in firmware settings'}` : secureBoot?.reason ?? 'Waiting for the agent'} />
            </div>
          </Panel>
          <Panel>
            <PanelHeading title="Sensor coverage" right={<Chip>{coverage.available} / {coverage.total}</Chip>} />
            <div>
              {coverage.providers.map((p) => <Spec key={p.id} label={p.label} value={`${p.available} / ${p.total} sensors`} tone={p.available ? 'default' : 'na'} />)}
            </div>
            <p className="note">Temperature, clock, power, capacity and utilization are mapped to physical components. Unavailable sensors are counted, never estimated.</p>
            <Button icon="settings2" onClick={() => navigate('settings')} style={{ alignSelf: 'flex-start' }}>Manage sensor providers</Button>
          </Panel>
        </div>
      </div>
      <div className="split split--rev">
        <PosturePanel />
        <DeviceEventsPanel />
      </div>
    </>
  )
}
