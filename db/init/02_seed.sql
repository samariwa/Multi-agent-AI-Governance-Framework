-- Seed data: 12 rows per table. All names and numbers are fictional.

INSERT INTO tickets (subject, status, created_at, customer_name, internal_notes) VALUES
    ('Cannot reset password',                          'open',    '2026-10-01T09:12:00Z', 'Harper Logistics',   'Customer called twice.'),
    ('Invoice shows wrong VAT rate',                   'open',    '2026-10-02T14:30:00Z', 'Nimbus Foods',       'Finance aware.'),
    ('URGENT: production checkout down for all users', 'open',    '2026-10-20T07:45:00Z', 'Quayside Retail',    'Escalated to on-call. Revenue impact ~£40k/hr.'),
    ('Feature request: dark mode',                     'closed',  '2026-09-12T10:00:00Z', 'Oakfield Studio',    NULL),
    ('Export to CSV truncates long fields',            'pending', '2026-10-05T16:20:00Z', 'Brightwater Health', 'Repro confirmed.'),
    ('SSO login loop after IdP change',                'open',    '2026-10-18T11:05:00Z', 'Carrow Insurance',   'Possibly SAML clock skew.'),
    ('Typo on pricing page',                           'closed',  '2026-09-20T08:40:00Z', 'Fennick & Sons',     NULL),
    ('API rate limit too low for nightly sync',        'pending', '2026-10-10T13:15:00Z', 'Harper Logistics',   'Sales wants to upsell.'),
    ('Data breach concern: other customer data shown', 'open',    '2026-10-21T19:30:00Z', 'Brightwater Health', 'Security team notified. Possible cache bug.'),
    ('Mobile app crashes on Android 15',               'open',    '2026-10-15T09:55:00Z', 'Oakfield Studio',    NULL),
    ('Request to delete account',                      'closed',  '2026-09-28T12:00:00Z', 'Nimbus Foods',       'GDPR erasure completed.'),
    ('Slow dashboard load times',                      'open',    '2026-10-12T15:10:00Z', 'Quayside Retail',    'Perf team investigating.');

INSERT INTO employees (name, department, salary, bank_account) VALUES
    ('Ada Okafor',       'Engineering', 92000, 'GB29 NWBK 6016 1331 9268 19'),
    ('Ben Hartley',      'Engineering', 78000, 'GB94 BARC 1020 1530 0934 59'),
    ('Chloe Nakamura',   'Support',     41000, 'GB33 BUKB 2020 1555 5555 55'),
    ('Dev Patel',        'Support',     39500, 'GB82 WEST 1234 5698 7654 32'),
    ('Elena Rossi',      'HR',          58000, 'GB15 MIDL 4005 1512 3456 78'),
    ('Farid Haddad',     'Finance',     67000, 'GB47 LOYD 3096 3412 3456 78'),
    ('Grace Liu',        'Engineering', 105000,'GB71 HBUK 4012 3412 3456 78'),
    ('Hugo Martins',     'Sales',       54000, 'GB10 NWBK 6016 1331 9268 20'),
    ('Isla Murray',      'Sales',       61000, 'GB55 BARC 2004 1599 7612 34'),
    ('Jonas Weber',      'Support',     43000, 'GB12 BUKB 2020 1555 1234 56'),
    ('Kemi Adebayo',     'Finance',     72000, 'GB68 MIDL 4005 1598 7654 32'),
    ('Liam O''Connell',  'HR',          49000, 'GB03 LOYD 3096 3498 7654 32');
