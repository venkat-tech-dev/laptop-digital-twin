import { navigate } from '../app/routes'
import { auth } from '../services/auth'
import { useSession } from '../stores/sessionStore'
import { PageHeading } from '../ui/PageHeading'
import { Chip } from '../ui/primitives'
import { OrgAudit } from '../org/OrgAudit'
import { OrgDevices } from '../org/OrgDevices'
import { OrgIdentity } from '../org/OrgIdentity'
import { OrgMembers } from '../org/OrgMembers'
import { OrgOverview } from '../org/OrgOverview'
import { OrgPlatform } from '../org/OrgPlatform'
import { OrgPolicies } from '../org/OrgPolicies'
import { OrgStructure } from '../org/OrgStructure'
import { useCan } from '../org/orgUtils'

type Section = 'overview' | 'devices' | 'members' | 'structure' | 'policies' | 'identity' | 'audit' | 'platform'

const SECTIONS: { id: Section; label: string; needs: string | null }[] = [
  { id: 'overview', label: 'Overview', needs: 'device.view' },
  { id: 'devices', label: 'Devices & enrollment', needs: 'device.view' },
  { id: 'members', label: 'Members', needs: 'user.view' },
  { id: 'structure', label: 'Structure & groups', needs: 'group.view' },
  { id: 'policies', label: 'Policies', needs: 'policy.view' },
  { id: 'identity', label: 'Identity & security', needs: null },
  { id: 'audit', label: 'Audit', needs: 'audit.view' },
  { id: 'platform', label: 'Platform', needs: 'platform.manage' },
]

/**
 * Organization administration (Phase 9). Tabs follow the signed-in member's permissions; the server
 * authorizes every request again, so hiding a tab is a convenience, never the control.
 */
export function OrganizationPage({ section }: { section?: string | null }) {
  const me = useSession((s) => s.me)
  const can = useCan()
  const visible = SECTIONS.filter((s) => s.needs === null || can(s.needs))
  // the URL (#/organization/<section>) is the selected tab
  const tab: Section = visible.find((s) => s.id === section)?.id ?? visible[0]?.id ?? 'identity'

  if (auth.mode !== 'accounts' || !me?.organization) {
    return (
      <>
        <PageHeading title="Organization" subtitle="Multi-tenant administration" />
        <section className="panel">
          <p className="note">Organizations, members, enrollment tokens, policies and audit need user accounts. Set <span className="caption-mono">AUTH_MODE=accounts</span> and a <span className="caption-mono">JWT_SECRET</span> in .env and restart the backend.</p>
        </section>
      </>
    )
  }

  return (
    <>
      <PageHeading
        title={me.organization.name}
        subtitle={<>Organization <span className="caption-mono">{me.organization.org_id}</span> · you are {me.org_role_label ?? me.org_role ?? '—'}{me.platform_admin ? ' · platform administrator' : ''}</>}
        action={<Chip tone={me.mfa ? 'accent' : 'amber'} title="Multi-factor authentication of this session">{me.mfa ? 'MFA SESSION' : 'NO MFA'}</Chip>}
      />
      <div className="seg seg--tabs" role="tablist" aria-label="Organization sections">
        {visible.map((s) => (
          <button key={s.id} type="button" role="tab" aria-selected={tab === s.id} className={`seg__btn ${tab === s.id ? 'is-on' : ''}`}
            onClick={() => navigate('organization', s.id)}>{s.label}</button>
        ))}
      </div>
      {tab === 'overview' ? <OrgOverview /> : null}
      {tab === 'devices' ? <OrgDevices /> : null}
      {tab === 'members' ? <OrgMembers /> : null}
      {tab === 'structure' ? <OrgStructure /> : null}
      {tab === 'policies' ? <OrgPolicies /> : null}
      {tab === 'identity' ? <OrgIdentity /> : null}
      {tab === 'audit' ? <OrgAudit /> : null}
      {tab === 'platform' ? <OrgPlatform /> : null}
    </>
  )
}
