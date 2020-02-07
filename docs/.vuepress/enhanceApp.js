export default ({
  router
}) => {
  router.addRoutes([
    // redirect from `guide/core_concepts` to `core/concepts`
    {
      path: '/guide/core_concepts/*',
      redirect: '/core/concepts/*'
    },
    // redirect any other `/guide` route to a `/core` route
    {
      path: '/guide/*',
      redirect: '/core/*'
    },
    // redirect from `cloud/cloud_concepts` to `cloud/concepts`
    {
      path: '/cloud/cloud_concepts/*',
      redirect: '/cloud/concepts/*'
    },
    // redirect from `api/unreleased` to `api/latest`
    {
      path: '/api/unreleased/*',
      redirect: '/api/latest/*'
    },
    // redirect from `core/tutorials` to `core/references`
    {
      path: '/core/tutorials/*',
      redirect: '/core/advanced-tutorials/*'
    },
    // redirect from `core/examples` to `core/reference_examples`
    {
      path: '/core/examples/*',
      redirect: '/core/reference_examples/*'
    }
  ])
}