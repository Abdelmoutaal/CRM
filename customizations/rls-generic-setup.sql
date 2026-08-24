-- ============================================================
-- RLS générique Twenty CRM
-- Règle : tout rôle NON-admin (canUpdateAllSettings = false)
--         ne voit, sur chaque objet métier (non-système),
--         que les enregistrements dont createdBy = lui-même.
-- Idempotent : ré-exécutable sans créer de doublons.
-- ============================================================

WITH wm_id_field AS (
    SELECT fm.id AS id
    FROM core."fieldMetadata" fm
    JOIN core."objectMetadata" om ON om.id = fm."objectMetadataId"
    WHERE om."nameSingular" = 'workspaceMember'
      AND fm.name = 'id'
),
core_app AS (
    SELECT om2."applicationId" AS id
    FROM core."objectMetadata" om2
    WHERE om2."nameSingular" = 'company'
),
targets AS (
    SELECT r.id        AS role_id,
           r."workspaceId" AS workspace_id,
           om.id       AS object_metadata_id,
           fm.id       AS field_metadata_id,
           om."nameSingular" AS object_name
    FROM core."role" r
    CROSS JOIN core."objectMetadata" om
    JOIN core."fieldMetadata" fm
      ON fm."objectMetadataId" = om.id AND fm.name = 'createdBy'
    WHERE r."canUpdateAllSettings" = false   -- tous les rôles sauf Admin
      AND om."isSystem" = false              -- objets métier uniquement
)
INSERT INTO core."rowLevelPermissionPredicate" (
    "universalIdentifier",
    "applicationId",
    id,
    "fieldMetadataId",
    "objectMetadataId",
    operand,
    value,
    "subFieldName",
    "workspaceMemberFieldMetadataId",
    "workspaceMemberSubFieldName",
    "rowLevelPermissionPredicateGroupId",
    "positionInRowLevelPermissionPredicateGroup",
    "workspaceId",
    "roleId",
    "createdAt",
    "updatedAt"
)
SELECT gen_random_uuid(),
       (SELECT id FROM core_app),
       gen_random_uuid(),
       t.field_metadata_id,
       t.object_metadata_id,
       'IS',
       NULL,
       'workspaceMemberId',
       (SELECT id FROM wm_id_field),
       NULL,
       NULL,
       NULL,
       t.workspace_id,
       t.role_id,
       now(),
       now()
FROM targets t
WHERE NOT EXISTS (
    SELECT 1
    FROM core."rowLevelPermissionPredicate" p
    WHERE p."roleId" = t.role_id
      AND p."objectMetadataId" = t.object_metadata_id
      AND p."fieldMetadataId" = t.field_metadata_id
      AND p."deletedAt" IS NULL
);

-- Vérification
SELECT r.label AS role_label, om."nameSingular" AS object_name, p.operand, p."subFieldName"
FROM core."rowLevelPermissionPredicate" p
JOIN core."role" r ON r.id = p."roleId"
JOIN core."objectMetadata" om ON om.id = p."objectMetadataId"
WHERE p."deletedAt" IS NULL
ORDER BY om."nameSingular";
