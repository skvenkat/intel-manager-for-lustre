# This file will selectively autopass some commits.

# At present bumps to the GUI only are auto passed.

. chroma-manager/tests/framework/utils/gui_update.sh
. chroma-manager/tests/framework/utils/fake_pass.sh

check_for_autopass() {
    # currently defined tests:
    # integration-tests-shared-storage-configuration-with-simulator
    # integration-tests-existing-filesystem-configuration
    # integration-tests-shared-storage-configuration
    # test-services
    # upgrade-tests
    # unit-tests
    # vvvvvvvvvvv this should come from a pragma in the commit message
    ALL_TESTS="integration-tests-existing-filesystem-configuration
               integration-tests-shared-storage-configuration
               integration-tests-shared-storage-configuration-with-simulator
               test-services
               unit-tests
               upgrade-tests"
    commit_message=$(git log -n 1)
    TESTS_TO_RUN=$(echo "$commit_message" | sed -ne '/^ *Run-tests:/s/^ *Run-tests: *//p')
    if [ -n "$TESTS_TO_RUN" ]; then
        TESTS_TO_SKIP=$ALL_TESTS
        for t in $TESTS_TO_RUN; do
            TESTS_TO_SKIP=${TESTS_TO_SKIP/$t/}
        done
    else
        TESTS_TO_SKIP=$(echo "$commit_message" | sed -ne '/^ *Skip-tests:/s/^ *Skip-tests: *//p')
    fi
    for t in $TESTS_TO_SKIP; do
        if [[ $JOB_NAME == $t || $JOB_NAME == $t/* ]]; then
            echo "skipping this test due to {Run|Skip}-tests pragma"
            fake_test_pass "tests_skipped_because_commit_pragma" "$WORKSPACE/test_reports/" ${BUILD_NUMBER}
            exit 1
        fi
    done

    tests_required_for_gui_bumps="chroma-tests-services"

    if [[ $BUILD_JOB_NAME = *-reviews ]] && gui_bump && [[ ! $tests_required_for_gui_bumps = $JOB_NAME ]]; then
      fake_test_pass "tests_skipped_because_gui_version_bump" "$WORKSPACE/test_reports/" ${BUILD_NUMBER}
      exit 0
    fi

    # regex matches separated by |
    supported_distro_versions="7\.[0-9]+"
    if [[ ! $TEST_DISTRO_VERSION =~ $supported_distro_versions ]] && ([ -z "$UPGRADE_TEST_DISTRO" ] || [[ ! $UPGRADE_TEST_DISTRO =~ $supported_distro_versions ]]); then
      fake_test_pass "tests_skipped_because_unsupported_distro_$TEST_DISTRO_VERSION" "$WORKSPACE/test_reports/" ${BUILD_NUMBER}
      exit 0
    fi

    # RHEL 7.5 won't upgrade CentOS 7.3
    if [[ ($JOB_NAME == upgrade-tests || $JOB_NAME == upgrade-tests/*) &&
        $TEST_DISTRO_NAME != rhel ]]; then
        fake_test_pass "upgrade-tests_skipped_on_centos7.3" "$WORKSPACE/test_reports/" "${BUILD_NUMBER}"
        exit 0
    fi

}  # end of check_for_autopass()
